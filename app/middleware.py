"""Inbound rate limiting and a short response cache for the public API."""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

EXEMPT_PREFIXES = ("/healthz", "/static")


def client_ip(request: Request) -> str:
    """First X-Forwarded-For entry (Render's proxy), else the socket address."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


class SlidingWindowLimiter:
    """In-memory per-client limiter (single instance)."""

    def __init__(
        self,
        limit: int = 60,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, int]:
        """Record a hit. Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False, max(1, math.ceil(self.window - (now - q[0])))
        q.append(now)
        if len(self._hits) > 5000:  # drop idle clients
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window]:
                del self._hits[k]
        return True, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: SlidingWindowLimiter) -> None:
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)
        allowed, retry = self.limiter.check(client_ip(request))
        if not allowed:
            return JSONResponse(
                {
                    "error": "rate_limited",
                    "detail": f"Limit is {self.limiter.limit} requests per minute per client. "
                    f"Retry in {retry}s.",
                },
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        return await call_next(request)


class CacheMiddleware(BaseHTTPMiddleware):
    """Caches successful GET /api/v1/* responses for `ttl` seconds (keyed by full URL)."""

    def __init__(self, app, ttl: float = 30.0, clock: Callable[[], float] = time.monotonic):
        super().__init__(app)
        self.ttl = ttl
        self._clock = clock
        self._store: dict[str, tuple[float, bytes, str]] = {}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self.ttl <= 0 or request.method != "GET" or not request.url.path.startswith("/api/v1/"):
            return await call_next(request)
        key = str(request.url)
        now = self._clock()
        hit = self._store.get(key)
        headers = {"Cache-Control": f"public, max-age={int(self.ttl)}"}
        if hit and now - hit[0] < self.ttl:
            return Response(
                hit[1],
                media_type=hit[2],
                headers={**headers, "X-Cache": "HIT", "Age": str(int(now - hit[0]))},
            )
        resp = await call_next(request)
        if resp.status_code != 200:
            return resp
        body = b"".join([chunk async for chunk in resp.body_iterator])  # type: ignore[attr-defined]
        media = resp.media_type or "application/json"
        if len(self._store) > 500:
            self._store = {k: v for k, v in self._store.items() if now - v[0] < self.ttl}
        self._store[key] = (now, body, media)
        return Response(
            body,
            status_code=200,
            media_type=media,
            headers={**headers, "X-Cache": "MISS"},
        )
