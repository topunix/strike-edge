"""Outbound HTTP: token-bucket limiter, retries with backoff, degraded tracking.

One UpstreamClient per venue, shared by the poller and the history backfill so a
single budget covers all outbound calls to that venue.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger("strikefast.upstream")


class UpstreamError(Exception):
    """A request failed after retries."""


class UpstreamDegraded(UpstreamError):
    """The venue is in a rate-limit cool-down; no request was sent."""


class TokenBucket:
    """Async token bucket. `capacity` tokens, refilled at `refill_per_sec`.

    Cost larger than capacity is clamped so a single oversized request can still run.
    """

    def __init__(
        self,
        capacity: float,
        refill_per_sec: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if capacity <= 0 or refill_per_sec <= 0:
            raise ValueError("capacity and refill must be positive")
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.refill_per_sec)
        self._last = now

    async def acquire(self, cost: float = 1.0) -> float:
        """Wait until `cost` tokens are available. Returns total seconds waited."""
        cost = min(cost, self.capacity)
        waited = 0.0
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= cost:
                    self._tokens -= cost
                    return waited
                need = (cost - self._tokens) / self.refill_per_sec
                await self._sleep(need)
                waited += need

    @classmethod
    def per_window(
        cls,
        limit: float,
        window_seconds: float,
        fraction: float = 0.5,
        burst_fraction: float = 0.1,
        **kw: Any,
    ) -> TokenBucket:
        """Bucket allowing `fraction` of a published `limit` per `window_seconds`.

        Capacity is only `burst_fraction` of the budget (at least 1 token), so in any
        window the traffic is at most budget * (1 + burst_fraction), not 2x budget.
        """
        budget = limit * fraction
        return cls(max(budget * burst_fraction, 1.0), budget / window_seconds, **kw)


class UpstreamClient:
    """Rate-limited, retrying HTTP client for one venue."""

    def __init__(
        self,
        venue: str,
        bucket: TokenBucket,
        http: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        max_inline_wait: float = 10.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        endpoint_buckets: dict[str, TokenBucket] | None = None,
    ) -> None:
        self.venue = venue
        self.bucket = bucket
        self.endpoint_buckets = endpoint_buckets or {}
        self.last_rate_limited_ts: float | None = None
        self.http = http or httpx.AsyncClient(timeout=timeout)
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.max_inline_wait = max_inline_wait
        self._clock = clock
        self._sleep = sleep
        self.degraded_until: float = 0.0
        self.last_error: str | None = None
        self.last_error_ts: float | None = None
        self.last_success_ts: float | None = None
        self.counts: Counter[int] = Counter()  # epoch minute -> requests sent

    # -- status ---------------------------------------------------------
    @property
    def degraded(self) -> bool:
        return self._clock() < self.degraded_until

    def requests_last_minute(self) -> int:
        minute = int(self._clock() // 60)
        return self.counts[minute] + self.counts[minute - 1]

    def request_counts(self, minutes: int = 10) -> dict[int, int]:
        """Requests sent per epoch minute for the most recent `minutes` minutes."""
        now_min = int(self._clock() // 60)
        return {m: c for m, c in sorted(self.counts.items()) if m > now_min - minutes}

    def _count(self) -> None:
        minute = int(self._clock() // 60)
        self.counts[minute] += 1
        for old in [m for m in self.counts if m < minute - 30]:
            del self.counts[old]

    def _fail(self, msg: str) -> None:
        self.last_error = msg
        self.last_error_ts = self._clock()

    def _backoff(self, attempt: int) -> float:
        return self.backoff_base * (2**attempt) * (0.5 + random.random() / 2)  # noqa: S311

    # -- request --------------------------------------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        weight: float = 1.0,
        endpoint: str | None = None,
        **kw: Any,
    ) -> Any:
        """Send a request and return parsed JSON. Raises UpstreamError on failure.

        `endpoint` selects an extra per-endpoint bucket (cost 1) on top of the venue bucket.
        """
        if self.degraded:
            raise UpstreamDegraded(f"{self.venue} degraded until {self.degraded_until:.0f}")
        last_exc: Exception | None = None
        for attempt in range(self.max_attempts):
            await self.bucket.acquire(weight)
            if endpoint and endpoint in self.endpoint_buckets:
                await self.endpoint_buckets[endpoint].acquire(1)
            self._count()
            try:
                resp = await self.http.request(method, url, **kw)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                self._fail(f"{type(exc).__name__}: {exc}")
                await self._sleep(self._backoff(attempt))
                continue
            if resp.status_code in (429, 418):
                wait = self._retry_after(resp, attempt)
                self.degraded_until = self._clock() + wait
                self.last_rate_limited_ts = self._clock()
                self._fail(f"HTTP {resp.status_code}; backing off {wait:.1f}s")
                log.warning(
                    "%s rate limited (%s), backoff %.1fs", self.venue, resp.status_code, wait
                )
                if wait > self.max_inline_wait or attempt == self.max_attempts - 1:
                    raise UpstreamDegraded(self.last_error or "rate limited")
                await self._sleep(wait)
                self.degraded_until = 0.0
                continue
            if resp.status_code >= 500:
                self._fail(f"HTTP {resp.status_code}")
                last_exc = UpstreamError(f"HTTP {resp.status_code}")
                await self._sleep(self._backoff(attempt))
                continue
            if resp.status_code >= 400:
                self._fail(f"HTTP {resp.status_code}: {resp.text[:120]}")
                raise UpstreamError(self.last_error)
            self.last_success_ts = self._clock()
            try:
                return resp.json()
            except ValueError as exc:
                self._fail(f"invalid JSON: {exc}")
                raise UpstreamError(self.last_error) from exc
        raise UpstreamError(self.last_error or f"{self.venue} request failed: {last_exc}")

    def _retry_after(self, resp: httpx.Response, attempt: int) -> float:
        header = resp.headers.get("retry-after")
        if header:
            try:
                return max(0.0, float(header))
            except ValueError:
                pass
        return self._backoff(attempt + 1)

    async def get(self, url: str, **kw: Any) -> Any:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> Any:
        return await self.request("POST", url, **kw)

    async def aclose(self) -> None:
        await self.http.aclose()
