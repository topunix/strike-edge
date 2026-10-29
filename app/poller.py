"""60 second poller. Per-venue failure isolation, staggered order-book fetches.

Each cycle every venue (concurrently): one batch quote call, then one order book per
matched market, spaced evenly across `stagger_seconds` so requests never burst.
A venue failure is recorded and never stops the other venues.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.adapters.base import UpstreamError
from app.config import HYPERLIQUID, OKX, STRIKE, MarketMap, Settings
from app.db import Store
from app.engine import Engine
from app.state import LiveState

log = logging.getLogger("strikeedge.poller")
SNAPSHOT_KEEP_MS = 7 * 24 * 3600 * 1000
LIQUIDITY_KEEP_MS = 40 * 24 * 3600 * 1000  # covers the 30 day series with margin


class Poller:
    def __init__(
        self,
        settings: Settings,
        state: LiveState,
        store: Store | None,
        adapters: dict[str, Any],
        markets: list[MarketMap],
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        cycle_seconds: float = 60.0,
        stagger_seconds: float = 50.0,
    ) -> None:
        self.settings = settings
        self.state = state
        self.store = store
        self.adapters = {v: a for v, a in adapters.items() if v in settings.enabled_venues}
        self.markets = markets
        self.now_ms = now_ms
        self._sleep = sleep
        self.cycle_seconds = cycle_seconds
        self.stagger_seconds = stagger_seconds

    def _book_targets(self, venue: str) -> list[tuple[str, str]]:
        """(strike symbol, venue-native symbol) for every matched market on a venue."""
        out = []
        for m in self.markets:
            if venue == STRIKE:
                out.append((m.strike, m.strike))
            elif venue == HYPERLIQUID and m.hl_coin:
                out.append((m.strike, m.hl_coin))
            elif venue == OKX and m.okx_inst:
                out.append((m.strike, m.okx_inst))
        return out

    async def _venue_cycle(self, venue: str) -> None:
        adapter = self.adapters[venue]
        try:
            quotes = await adapter.fetch_quotes()
            self.state.set_quotes(venue, quotes, self.now_ms())
            if venue == STRIKE:
                self.state.tradfi = dict(getattr(adapter, "tradfi", {}))
        except Exception as exc:  # isolated: other venues keep going
            self.state.record_error(venue, f"{type(exc).__name__}: {exc}", self.now_ms())
            level = logging.WARNING if isinstance(exc, UpstreamError) else logging.ERROR
            log.log(level, "%s quote fetch failed: %s", venue, exc)
            return  # without quotes there is no point hammering the order books
        targets = [t for t in self._book_targets(venue) if t[0] in quotes]
        if not targets:
            return
        spacing = self.stagger_seconds / len(targets)
        for i, (symbol, native) in enumerate(targets):
            if i:
                await self._sleep(spacing)
            try:
                book = await adapter.fetch_book(native)
                self.state.set_book(venue, symbol, book)
            except Exception as exc:
                self.state.record_error(venue, f"book {native}: {exc}", self.now_ms())
                log.warning("%s book %s failed: %s", venue, native, exc)

    async def _liquidity_cycle(self) -> None:
        """Debt per collateral and SLP TVL, stored every cycle. Failure never stops quotes."""
        adapter = self.adapters.get(STRIKE)
        if self.store is None or not hasattr(adapter, "fetch_liquidity"):
            return
        try:
            sample = await adapter.fetch_liquidity()
            await asyncio.to_thread(self._save_liquidity, sample)
        except Exception as exc:  # isolated: the series just gets a gap, venue health is unchanged
            level = logging.WARNING if isinstance(exc, UpstreamError) else logging.ERROR
            log.log(level, "strike liquidity fetch failed: %s", exc)

    def _save_liquidity(self, sample) -> None:
        assert self.store is not None
        self.store.save_liquidity(sample)
        self.store.prune_liquidity(sample.ts_ms - LIQUIDITY_KEEP_MS)

    async def run_cycle(self) -> None:
        await asyncio.gather(
            self._liquidity_cycle(), *(self._venue_cycle(v) for v in self.adapters)
        )
        now = self.now_ms()
        self.state.cycles += 1
        self.state.last_cycle_ms = now
        if self.store is not None:
            try:
                await asyncio.to_thread(self._save_snapshot, now)
            except Exception:
                log.exception("snapshot save failed")

    def _save_snapshot(self, now: int) -> None:
        assert self.store is not None
        self.store.save_snapshot(now, self.snapshot_payload(now))
        self.store.prune_snapshots(now - SNAPSHOT_KEEP_MS)

    def snapshot_payload(self, now: int) -> dict:
        """Compact record: quotes plus the ranked opportunities at default parameters."""
        s = self.settings
        engine = Engine(s, self.state, self.store, lambda: now)
        ranked, _ = engine.opportunities(s.default_notional, s.default_hours, "taker")
        quotes = {
            v: {
                sym: {
                    "mark": q.mark_price,
                    "funding_rate_per_hour": q.funding.rate_per_hour if q.funding else None,
                    "source_ts_ms": q.source_ts_ms,
                }
                for sym, q in qs.items()
            }
            for v, qs in self.state.quotes.items()
            if qs
        }
        return {
            "assumptions": {
                "notional_usd": s.default_notional,
                "hours": s.default_hours,
                "fee_mode": "taker",
            },
            "quotes": quotes,
            "opportunities": [
                {
                    "symbol": r.market.strike,
                    "long_venue": r.long_venue,
                    "short_venue": r.short_venue,
                    "net_usd": r.two_leg.net_usd,
                    "carry_usd_per_hour": r.two_leg.carry_per_hour_usd,
                    "confidence": r.confidence,
                    "rank": r.rank,
                }
                for r in ranked
            ],
        }

    async def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.run_cycle()
            except Exception:
                log.exception("poll cycle failed")
            await self._sleep(max(1.0, self.cycle_seconds - (time.monotonic() - started)))
