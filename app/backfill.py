"""Background funding-history backfill.

Throttled by the shared per-venue UpstreamClient buckets, resumable (state is stored
per venue and market after each job) and skips data already cached. Venues run
concurrently; jobs within a venue run one at a time. A failure in one job or venue
never stops the others.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.adapters.base import UpstreamError
from app.config import HYPERLIQUID, OKX, STRIKE, MarketMap
from app.db import BackfillState, Store
from app.liquidity import BACKFILL_DAYS
from app.models import FundingPoint, LiquiditySample

log = logging.getLogger("strikeedge.backfill")
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
LIQUIDITY_KEY = "liquidity"  # backfill_state symbol for the Strike liquidity series
LIQUIDITY_DAYS = BACKFILL_DAYS
LIQUIDITY_REFRESH_MS = 6 * HOUR_MS


class HistorySource(Protocol):
    async def fetch_funding_history(
        self, market: MarketMap, start_ms: int, end_ms: int
    ) -> list[FundingPoint]: ...


def has_market(venue: str, m: MarketMap) -> bool:
    return {STRIKE: True, HYPERLIQUID: bool(m.hl_coin), OKX: bool(m.okx_inst)}[venue]


@dataclass
class Progress:
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    running: bool = False
    finished_ms: int | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def loading(self) -> bool:
        return self.running or self.finished_ms is None


class Backfiller:
    def __init__(
        self,
        store: Store,
        sources: dict[str, HistorySource],
        markets: list[MarketMap],
        days: int = 30,
        refresh_after_ms: int = 30 * 60_000,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.store = store
        self.sources = sources
        self.markets = markets
        self.days = days
        self.refresh_after_ms = refresh_after_ms
        self.now_ms = now_ms
        self.progress = Progress()

    def _jobs(self, venue: str) -> list[MarketMap]:
        return [m for m in self.markets if has_market(venue, m)]

    async def run(self) -> Progress:
        """One full pass over every venue and market."""
        p = self.progress
        p.total = sum(len(self._jobs(v)) for v in self.sources)
        p.done = p.failed = p.skipped = 0
        p.errors = {}
        p.running = True
        try:
            await asyncio.gather(*(self._run_venue(v) for v in self.sources))
        finally:
            p.running = False
            p.finished_ms = self.now_ms()
        return p

    async def _run_venue(self, venue: str) -> None:
        for m in self._jobs(venue):
            try:
                await self._job(venue, m)
            except Exception as exc:  # isolate: one bad market must not stop the rest
                self._record_failure(venue, m, exc)

    def _record_failure(self, venue: str, m: MarketMap, exc: Exception) -> None:
        p = self.progress
        p.failed += 1
        msg = f"{type(exc).__name__}: {exc}"
        p.errors[f"{venue}:{m.strike}"] = msg
        old = self.store.get_state(venue, m.strike)
        self.store.set_state(
            BackfillState(
                venue,
                m.strike,
                old.covered_from_ms if old else None,
                old.covered_to_ms if old else None,
                old.last_checked_ms if old else None,
                "error",
                msg[:300],
            )
        )
        level = logging.WARNING if isinstance(exc, UpstreamError) else logging.ERROR
        log.log(level, "backfill %s %s failed: %s", venue, m.strike, msg)

    async def _job(self, venue: str, m: MarketMap) -> None:
        now = self.now_ms()
        window_start = now - self.days * DAY_MS
        st = self.store.get_state(venue, m.strike)
        ok = st is not None and st.status == "ok" and st.covered_to_ms is not None
        covers = ok and st.covered_from_ms is not None and st.covered_from_ms <= window_start
        if covers and st.last_checked_ms and now - st.last_checked_ms < self.refresh_after_ms:
            self.progress.skipped += 1
            self.progress.done += 1
            return
        # Incremental when the cached range already reaches back to the window start.
        start = st.covered_to_ms + 1 if covers else window_start
        points = await self.sources[venue].fetch_funding_history(m, start, now)
        await asyncio.to_thread(self.store.upsert_funding, venue, m.strike, points)
        covered_from = st.covered_from_ms if covers else window_start
        known_to = [p.ts_ms for p in points] + ([st.covered_to_ms] if ok else [])
        covered_to = max(known_to) if known_to else None
        self.store.set_state(
            BackfillState(venue, m.strike, covered_from, covered_to, now, "ok", None)
        )
        self.store.prune_funding(now - (self.days + 5) * DAY_MS)
        self.progress.done += 1

    async def run_liquidity(self) -> int:
        """Backfill SLP TVL, share price and daily volume for the liquidity quality series.

        Debt has no upstream history, so it is never backfilled. Existing values (live
        samples) are never overwritten. Returns the number of rows written.
        """
        src = self.sources.get(STRIKE)
        if src is None or not hasattr(src, "fetch_slp_history"):
            return 0
        now = self.now_ms()
        start = now - LIQUIDITY_DAYS * DAY_MS
        st = self.store.get_state(STRIKE, LIQUIDITY_KEY)
        if st and st.status == "ok" and now - (st.last_checked_ms or 0) < LIQUIDITY_REFRESH_MS:
            return 0
        try:
            rows = await self._liquidity_rows(src, start, now)
            await asyncio.to_thread(self.store.fill_liquidity, rows)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            old = st
            self.store.set_state(
                BackfillState(
                    STRIKE, LIQUIDITY_KEY, old and old.covered_from_ms, old and old.covered_to_ms,
                    old and old.last_checked_ms, "error", msg[:300],
                )
            )  # fmt: skip
            level = logging.WARNING if isinstance(exc, UpstreamError) else logging.ERROR
            log.log(level, "liquidity backfill failed: %s", msg)
            return 0
        newest = max((r.ts_ms for r in rows), default=None)
        self.store.set_state(BackfillState(STRIKE, LIQUIDITY_KEY, start, newest, now, "ok", None))
        return len(rows)

    async def _liquidity_rows(self, src, start: int, now: int) -> list[LiquiditySample]:
        """Hourly (30d) rows plus daily (6m) rows for older days; volume on UTC midnights."""
        history: dict[int, tuple[float, float | None]] = {}
        errors: list[Exception] = []
        for period in ("30d", "6m"):  # hourly first: it wins where both have a row
            try:
                for ts, tvl, sp in await src.fetch_slp_history(period):
                    history.setdefault(ts, (tvl, sp))
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 2:
            raise errors[0]
        try:
            volumes = await src.fetch_daily_volume()
        except Exception as exc:  # volume is optional here; the next pass retries it
            log.warning("liquidity volume backfill failed: %s", exc)
            volumes = {}
        rows = []
        for ts, (tvl, sp) in sorted(history.items()):
            if not start <= ts <= now:
                continue
            # the daily total for day D belongs to the trailing 24h that ends at midnight D+1
            vol = volumes.get(ts - DAY_MS) if ts % DAY_MS == 0 else None
            rows.append(LiquiditySample(ts, tvl, sp, None, None, vol))
        return rows
