"""Combines live state, fees and history into View A and View B rows.

Used by both the JSON API and the HTML pages so they always agree.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app import calc, report
from app.config import (
    PRESETS_HOURS,
    STALE_AFTER_MS,
    STRIKE,
    SYMBOL_MAP,
    MarketMap,
    Settings,
)
from app.db import Store
from app.hours import market_status
from app.models import Book, Quote
from app.state import LiveState

DIRECTIONS = (report.LONG_STRIKE, report.LONG_OTHER)


@dataclass
class Leg:
    venue: str
    native_symbol: str
    quote: Quote | None
    book: Book | None
    fills: calc.FillEstimate | None
    rate_per_hour: float | None
    fee_rate: float
    stale: bool
    source_ts_ms: int | None  # quote source timestamp
    book_ts_ms: int | None

    @property
    def mark(self) -> float | None:
        return self.quote.mark_price if self.quote else None

    @property
    def ts_list(self) -> list[int | None]:
        return [self.source_ts_ms, self.book_ts_ms]


@dataclass
class VenueCost:
    """One venue's holding cost (View A) with everything needed to show the maths."""

    venue: str
    leg: Leg
    cost: calc.HoldingCost
    confidence: str
    confidence_reasons: list[str]
    rankable: bool
    exclusion: str | None


@dataclass
class HoldingResult:
    market: MarketMap
    side: str
    notional: float
    hours: float
    fee_mode: str
    venues: list[VenueCost]
    best: tuple[str, float] | None
    worst: tuple[str, float] | None
    gap_usd: float | None
    market_open: bool
    closed_reason: str | None


@dataclass
class OpportunityRow:
    market: MarketMap
    long_venue: str
    short_venue: str
    direction: str
    two_leg: calc.TwoLeg
    long_leg: Leg
    short_leg: Leg
    net_by_period: dict[str, float | None]
    confidence: str
    confidence_reasons: list[str]
    stale: bool
    stale_venues: list[str]
    ranked: bool = False
    rank: int | None = None
    exclusion: str | None = None
    market_open: bool = True
    closed_reason: str | None = None
    fee_unverified_venues: list[str] = field(default_factory=list)

    @property
    def other_venue(self) -> str:
        return self.short_venue if self.long_venue == STRIKE else self.long_venue


@dataclass
class StrikeOnlyRow:
    market: MarketMap
    quote: Quote | None
    leg: Leg
    is_tradfi: bool
    market_open: bool
    closed_reason: str | None
    stale: bool


class Engine:
    def __init__(
        self,
        settings: Settings,
        state: LiveState,
        store: Store | None = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        markets: dict[str, MarketMap] | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.store = store
        self.now_ms = now_ms
        self.markets = SYMBOL_MAP if markets is None else markets

    # -- helpers ---------------------------------------------------------
    def competitors(self, m: MarketMap) -> list[str]:
        out = []
        for venue, present in (("hyperliquid", bool(m.hl_coin)), ("okx", bool(m.okx_inst))):
            if present and venue in self.settings.enabled_venues:
                out.append(venue)
        return out

    def native(self, venue: str, m: MarketMap) -> str:
        return {STRIKE: m.strike, "hyperliquid": m.hl_coin, "okx": m.okx_inst}[venue] or ""

    def leg(self, venue: str, m: MarketMap, notional: float, fee_mode: str) -> Leg:
        now = self.now_ms()
        q = self.state.quotes.get(venue, {}).get(m.strike)
        b = self.state.books.get((venue, m.strike))
        fills = calc.estimate_fills(b.bids, b.asks, notional) if b else None
        stale = q is None or calc.is_stale(q.source_ts_ms, now)
        if b is not None and calc.is_stale(b.source_ts_ms, now):
            stale = True
        return Leg(
            venue=venue,
            native_symbol=self.native(venue, m),
            quote=q,
            book=b,
            fills=fills,
            rate_per_hour=q.funding.rate_per_hour if q and q.funding else None,
            fee_rate=self.settings.fee_rate(venue, fee_mode, m),
            stale=stale,
            source_ts_ms=q.source_ts_ms if q else None,
            book_ts_ms=b.source_ts_ms if b else None,
        )

    def _recent_rates(self, venue: str, symbol: str) -> dict[int, float]:
        """Hourly funding for the last 24 completed hours from the cached history."""
        if self.store is None:
            return {}
        hours = report.window_hours(self.now_ms(), 1)
        pts = self.store.get_funding(venue, symbol, hours[0] - 8 * report.HOUR)
        return report.hourly_series(pts, hours)

    def _hours24(self) -> list[int]:
        return report.window_hours(self.now_ms(), 1)

    def carry_consistency(self, m: MarketMap, long_venue: str, short_venue: str, sign: int):
        lr = self._recent_rates(long_venue, m.strike)
        sr = self._recent_rates(short_venue, m.strike)
        carry = [
            (sr[h] - lr[h]) if h in lr and h in sr else None for h in self._hours24()
        ]  # per-$ carry per hour
        return calc.sign_consistency(carry, sign)

    def rate_consistency(self, venue: str, m: MarketMap, sign: int):
        """Share of the last 24h with the same funding sign as now (zero means exactly 0)."""
        s = self._recent_rates(venue, m.strike)
        vals = [s.get(h) for h in self._hours24()]
        if sign == 0:
            known = [v for v in vals if v is not None]
            return sum(1 for v in known if v == 0) / len(known) if known else None
        return calc.sign_consistency(vals, sign)

    def hour_status(self, m: MarketMap) -> tuple[bool, str | None]:
        st = market_status(m.asset_class, self.now_ms())
        return st.open, st.reason

    # -- View A: holding cost ----------------------------------------------
    def holding_cost(
        self, m: MarketMap, side: str, notional: float, hours: float, fee_mode: str
    ) -> HoldingResult:
        now = self.now_ms()
        is_open, closed_reason = self.hour_status(m)
        maker = fee_mode == "maker"
        rows: list[VenueCost] = []
        for venue in (STRIKE, *self.competitors(m)):
            lg = self.leg(venue, m, notional, fee_mode)
            hc = calc.holding_cost(
                side, notional, hours, lg.fee_rate, lg.fills, lg.rate_per_hour, maker=maker
            )
            sign = (
                0 if lg.rate_per_hour is None else (lg.rate_per_hour > 0) - (lg.rate_per_hour < 0)
            )
            if sign == 0:  # zero funding: judge consistency against "not paying"
                sign = 1
            conf, reasons = calc.assess_confidence(
                lg.ts_list,
                now,
                hc.liquidity_ok,
                self.rate_consistency(venue, m, sign),
                market_open=is_open,
                closed_reason=closed_reason,
                ticker_only=m.ticker_only,
                fee_unverified=m.fee_unverified(venue),
            )
            excl = None
            if lg.stale:
                excl = "stale data"
            elif lg.quote is None or lg.rate_per_hour is None:
                excl = "no funding data"
            elif not hc.liquidity_ok:
                excl = "insufficient liquidity"
            rows.append(VenueCost(venue, lg, hc, conf, reasons, excl is None, excl))
        costs = {v.venue: v.cost.total_cost_usd if v.rankable else None for v in rows}
        best, worst, gap = calc.best_worst_for_direction(costs)
        return HoldingResult(
            m, side, notional, hours, fee_mode, rows, best, worst, gap, is_open, closed_reason
        )

    # -- View B: two-leg opportunities ---------------------------------------
    def opportunities(
        self,
        notional: float,
        hours: float,
        fee_mode: str,
        symbol: str | None = None,
        direction: str | None = None,
        min_net_usd: float | None = None,
        include_unverified: bool = False,
    ) -> tuple[list[OpportunityRow], list[OpportunityRow]]:
        """Returns (ranked, excluded). Excluded rows are still returned for the full list."""
        now = self.now_ms()
        maker = fee_mode == "maker"
        rows: list[OpportunityRow] = []
        for m in self.markets.values():
            if symbol and m.strike != symbol:
                continue
            comps = self.competitors(m)
            if not comps:
                continue
            strike_leg = self.leg(STRIKE, m, notional, fee_mode)
            is_open, closed_reason = self.hour_status(m)
            for other in comps:
                other_leg = self.leg(other, m, notional, fee_mode)
                for d in DIRECTIONS:
                    if direction and d != direction:
                        continue
                    long_leg, short_leg = (
                        (strike_leg, other_leg)
                        if d == report.LONG_STRIKE
                        else (other_leg, strike_leg)
                    )
                    tl = calc.two_leg(
                        notional,
                        hours,
                        long_leg.venue,
                        short_leg.venue,
                        long_leg.rate_per_hour,
                        short_leg.rate_per_hour,
                        long_leg.fee_rate,
                        short_leg.fee_rate,
                        long_leg.fills,
                        short_leg.fills,
                        long_leg.mark,
                        short_leg.mark,
                        maker=maker,
                    )
                    stale_venues = [lg.venue for lg in (long_leg, short_leg) if lg.stale]
                    sign = 0
                    if tl.carry_per_hour_usd is not None:
                        sign = (tl.carry_per_hour_usd > 0) - (tl.carry_per_hour_usd < 0)
                    cons = self.carry_consistency(m, long_leg.venue, short_leg.venue, sign)
                    unverified = [
                        lg.venue for lg in (long_leg, short_leg) if m.fee_unverified(lg.venue)
                    ]
                    conf, reasons = calc.assess_confidence(
                        long_leg.ts_list + short_leg.ts_list,
                        now,
                        tl.liquidity_ok,
                        cons,
                        market_open=is_open,
                        closed_reason=closed_reason,
                        ticker_only=m.ticker_only,
                        fee_unverified=bool(unverified),
                    )
                    nets = calc.preset_nets(
                        tl.carry_per_hour_usd, tl.fees_usd + tl.slippage_usd, PRESETS_HOURS
                    )
                    rows.append(
                        OpportunityRow(
                            market=m,
                            long_venue=long_leg.venue,
                            short_venue=short_leg.venue,
                            direction=d,
                            two_leg=tl,
                            long_leg=long_leg,
                            short_leg=short_leg,
                            net_by_period=nets,
                            confidence=conf,
                            confidence_reasons=reasons,
                            stale=bool(stale_venues),
                            stale_venues=stale_venues,
                            market_open=is_open,
                            closed_reason=closed_reason,
                            fee_unverified_venues=unverified,
                        )
                    )
        return self._rank(rows, include_unverified, min_net_usd)

    def _rank(
        self, rows: list[OpportunityRow], include_unverified: bool, min_net_usd: float | None
    ) -> tuple[list[OpportunityRow], list[OpportunityRow]]:
        by_id = {id(r): r for r in rows}
        opps = [
            calc.Opportunity(
                r.market.strike,
                r.long_venue,
                r.short_venue,
                r.two_leg.net_usd,
                stale=r.stale,
                liquidity_ok=r.two_leg.liquidity_ok,
                ticker_only=r.market.ticker_only,
                extra={"id": id(r)},
            )
            for r in rows
        ]
        ranked_opps = calc.rank_opportunities(opps, include_unverified)
        ranked: list[OpportunityRow] = []
        for i, o in enumerate(ranked_opps, start=1):
            r = by_id[o.extra["id"]]
            if min_net_usd is not None and (r.two_leg.net_usd or 0) < min_net_usd:
                continue
            r.ranked, r.rank = True, i
            ranked.append(r)
        taken = {id(r) for r in ranked}
        excluded = []
        for r in rows:
            if id(r) in taken:
                continue
            if r.stale:
                r.exclusion = "stale data"
            elif not r.two_leg.liquidity_ok:
                r.exclusion = "insufficient liquidity"
            elif r.two_leg.net_usd is None:
                r.exclusion = "missing funding data"
            elif r.market.ticker_only and not include_unverified:
                r.exclusion = "ticker match only (use include_unverified=true)"
            else:
                r.exclusion = f"below min_net_usd ({min_net_usd})"
            excluded.append(r)
        return ranked, excluded

    # -- Strike-only markets --------------------------------------------------
    def strike_only(self, notional: float) -> list[StrikeOnlyRow]:
        out = []
        for m in self.markets.values():
            if self.competitors(m):
                continue
            lg = self.leg(STRIKE, m, notional, "taker")
            is_open, reason = self.hour_status(m)
            out.append(
                StrikeOnlyRow(
                    m,
                    lg.quote,
                    lg,
                    self.state.tradfi.get(m.strike, False),
                    is_open,
                    reason,
                    lg.stale,
                )
            )
        return out

    # -- venue status ----------------------------------------------------------
    def venue_freshness(self, venue: str) -> tuple[int | None, bool]:
        ts = self.state.newest_source_ts(venue)
        return ts, calc.is_stale(ts, self.now_ms(), STALE_AFTER_MS)

    # -- report inputs ----------------------------------------------------------
    def report_slippage(self, notional: float) -> dict[tuple[str, str], report.SlippageEstimate]:
        floor = self.settings.report_slippage_bps_per_fill
        return {
            key: report.slippage_from_book(b, notional, floor)
            for key, b in self.state.books.items()
        }
