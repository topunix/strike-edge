"""Versioned JSON API (/api/v1), /healthz and /llms.txt."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app import calc, liquidity, report
from app.backfill import Backfiller
from app.config import ALL_VENUES, PRESETS_HOURS, STRIKE, Settings
from app.db import Store
from app.engine import Engine, OpportunityRow
from app.llms import llms_text
from app.middleware import SlidingWindowLimiter
from app.schemas import (
    DISCLAIMER,
    SCHEMA_VERSION,
    Assumptions,
    BackfillOut,
    CumPoint,
    Direction,
    ErrorResponse,
    FeeMode,
    HealthResponse,
    HistoryPoint,
    HistoryResponse,
    HoldingCostResponse,
    LegOut,
    LineItem,
    LiquidityQualityResponse,
    MarketSnapshot,
    OpportunitiesResponse,
    Opportunity,
    ReportResponse,
    Side,
    SnapshotResponse,
    SwitchResponse,
    VenueHealth,
    VenueHoldingCost,
    VenueQuote,
    VenueStatus,
)
from app.state import LiveState

ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}
router = APIRouter(responses=ERRORS)

SLIPPAGE_MODEL = (
    "Taker: walk each venue's order book for the notional; slippage = average fill vs book mid, "
    "on entry and exit. Maker: slippage assumed zero and fills are not guaranteed. A book too "
    "thin for the notional is marked insufficient liquidity and excluded from rankings."
)
FUNDING_MODEL = (
    "Current hourly-normalized funding rate is assumed to persist for the whole period. "
    "Positive rate: longs pay shorts. Funding, basis and liquidity change; these are estimates."
)


@dataclass
class Context:
    settings: Settings
    state: LiveState
    store: Store
    engine: Engine
    now_ms: Any
    backfiller: Backfiller | None = None
    clients: dict[str, Any] = field(default_factory=dict)
    limiter: SlidingWindowLimiter | None = None
    poller_started: bool = False
    _report_cache: tuple[int, dict] | None = None
    _liquidity_cache: tuple[int, dict] | None = None

    def liquidity(self, ttl_ms: int = 60_000) -> dict:
        """Liquidity quality dict, cached for a minute (shared by the API, MCP and web page)."""
        now = self.now_ms()
        if self._liquidity_cache and now - self._liquidity_cache[0] < ttl_ms:
            return self._liquidity_cache[1]
        since = now - liquidity.KEEP_DAYS * liquidity.DAY_MS
        thin = now - liquidity.THIN_AFTER_DAYS * liquidity.DAY_MS
        samples = self.store.get_liquidity(since, thin_before_ms=thin)
        out = liquidity.build(samples, self.settings.liquidity, now)
        out["flag"]["summary"] = liquidity.summary(out, self.settings.liquidity)
        self._liquidity_cache = (now, out)
        return out

    def report(self, ttl_ms: int = 60_000) -> dict:
        """Report dict at $10k, cached for a minute (shared by the API and the web pages)."""
        now = self.now_ms()
        if self._report_cache and now - self._report_cache[0] < ttl_ms:
            return self._report_cache[1]
        n = 10_000.0
        rep = report.build_report(
            self.store,
            self.settings,
            list(self.engine.markets.values()),
            now,
            notional=n,
            slippage=self.engine.report_slippage(n),
        )
        self._report_cache = (now, rep)
        return rep


def ctx_of(request: Request) -> Context:
    return request.app.state.ctx


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return (
        datetime.fromtimestamp(ms / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def norm_symbol(raw: str, ctx: Context) -> str:
    s = raw.strip().upper()
    if "-" not in s:
        s += "-USD"
    if s not in ctx.engine.markets:
        raise HTTPException(404, f"Unknown symbol {raw!r}. See /api/v1/snapshot for symbols.")
    return s


def venue_statuses(ctx: Context) -> dict[str, VenueStatus]:
    out = {}
    for v in ALL_VENUES:
        enabled = v in ctx.settings.enabled_venues
        ts, stale = ctx.engine.venue_freshness(v)
        client = ctx.clients.get(v)
        out[v] = VenueStatus(
            venue=v,
            enabled=enabled,
            stale=stale if enabled else True,
            degraded=bool(client and client.degraded),
            source_ts_ms=ts,
            source_ts_iso=iso(ts),
            age_seconds=None if ts is None else round((ctx.now_ms() - ts) / 1000, 1),
            last_error=ctx.state.last_error.get(v),
        )
    return out


def assumptions(ctx: Context, notional: float, hours: float, mode: str) -> Assumptions:
    s = ctx.settings
    return Assumptions(
        notional_usd=notional,
        hours=hours,
        fee_mode=mode,  # type: ignore[arg-type]
        fee_rates={v: s.fees[v][mode] for v in s.enabled_venues},
        preset_hours=PRESETS_HOURS,
        slippage_model=SLIPPAGE_MODEL,
        funding_model=FUNDING_MODEL,
        hl_hip3_fee_multiplier=s.hl_hip3_fee_multiplier,
        notes=[
            "Estimates, not profit or guaranteed arbitrage. Information only.",
            "Past funding does not predict future funding.",
            "Stale venue data (older than 5 minutes) is excluded from rankings.",
        ],
    )


def envelope(ctx: Context, notional: float, hours: float, mode: str) -> dict[str, Any]:
    now = ctx.now_ms()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_ms": now,
        "generated_at_iso": iso(now),
        "venues": venue_statuses(ctx),
        "assumptions": assumptions(ctx, notional, hours, mode),
        "disclaimer": DISCLAIMER,
    }


# --- query parameter types -----------------------------------------------------------
Notional = Annotated[
    float | None, Query(gt=0, le=100_000_000, description="Position size in USD.", examples=[10000])
]
Hours = Annotated[
    float | None, Query(gt=0, le=8760, description="Holding period in hours.", examples=[24])
]
Mode = Annotated[FeeMode, Query(description="taker (default) or maker ('if filled as maker').")]


def trade_url(ctx: Context) -> str:
    return ctx.settings.trade_url()


def leg_out(lg, hours_note: bool = False) -> LegOut:  # noqa: ARG001
    f = lg.quote.funding if lg.quote else None
    return LegOut(
        venue=lg.venue,
        native_symbol=lg.native_symbol,
        funding_rate_per_interval=f.rate_per_interval if f else None,
        funding_interval_hours=f.interval_hours if f else None,
        funding_rate_per_hour=lg.rate_per_hour,
        mark_price_usd=lg.mark,
        fee_rate=lg.fee_rate,
        stale=lg.stale,
        source_ts_ms=lg.source_ts_ms,
        source_ts_iso=iso(lg.source_ts_ms),
        liquidity_ok=None if lg.fills is None else lg.fills.sufficient,
    )


def line_items(r: OpportunityRow, notional: float, maker: bool) -> list[LineItem]:
    t = r.two_leg
    items = [
        LineItem(label=f"Funding, long leg on {r.long_venue}", usd=t.funding_long_usd),
        LineItem(label=f"Funding, short leg on {r.short_venue}", usd=t.funding_short_usd),
    ]
    for lg in (r.long_leg, r.short_leg):
        items.append(
            LineItem(
                label=f"Fees on {lg.venue} (entry + exit, {'maker' if maker else 'taker'})",
                usd=-2 * calc.fee_usd(notional, lg.fee_rate),
            )
        )
    for lg in (r.long_leg, r.short_leg):
        if maker:
            slip: float | None = 0.0
        elif lg.fills is None or not lg.fills.sufficient:
            slip = None
        else:
            assert (
                lg.fills.buy_slippage_frac is not None and lg.fills.sell_slippage_frac is not None
            )
            slip = -notional * (lg.fills.buy_slippage_frac + lg.fills.sell_slippage_frac)
        items.append(LineItem(label=f"Slippage on {lg.venue} (entry + exit)", usd=slip))
    items.append(LineItem(label="Estimated net", usd=t.net_usd))
    return items


def to_opportunity(ctx: Context, r: OpportunityRow, notional: float, maker: bool) -> Opportunity:
    t = r.two_leg
    return Opportunity(
        rank=r.rank,
        symbol=r.market.strike,
        asset_class=r.market.asset_class,
        direction=r.direction,  # type: ignore[arg-type]
        long_venue=r.long_venue,
        short_venue=r.short_venue,
        net_usd=t.net_usd,
        net_usd_by_period=r.net_by_period,
        carry_usd_per_hour=t.carry_per_hour_usd,
        funding_net_usd=t.funding_net_usd,
        fees_usd=t.fees_usd,
        slippage_usd=t.slippage_usd,
        round_trip_cost_usd=t.fees_usd + t.slippage_usd,
        break_even_hours=t.break_even_hours,
        basis_bps=t.basis_bps,
        confidence=r.confidence,  # type: ignore[arg-type]
        confidence_reasons=r.confidence_reasons,
        stale=r.stale,
        stale_venues=r.stale_venues,
        liquidity_ok=t.liquidity_ok,
        ticker_only=r.market.ticker_only,
        hl_fee_unverified=bool(r.fee_unverified_venues),
        market_open=r.market_open,
        market_closed_reason=r.closed_reason,
        exclusion_reason=r.exclusion,
        long_leg=leg_out(r.long_leg),
        short_leg=leg_out(r.short_leg),
        line_items=line_items(r, notional, maker),
        trade_url=trade_url(ctx),
        note=r.market.note,
    )


# --- endpoints --------------------------------------------------------------------------
@router.get(
    "/api/v1/snapshot",
    response_model=SnapshotResponse,
    tags=["v1"],
    summary="Live funding, marks, volume and depth for every Strike market",
)
def snapshot(
    request: Request,
    notional: Notional = None,
    hours: Hours = None,
) -> SnapshotResponse:
    ctx = ctx_of(request)
    n = notional or ctx.settings.default_notional
    h = hours or ctx.settings.default_hours
    eng = ctx.engine
    markets: list[MarketSnapshot] = []
    strike_only = {r.market.strike for r in eng.strike_only(n)}
    for m in eng.markets.values():
        is_open, reason = eng.hour_status(m)
        venues: dict[str, VenueQuote] = {}
        for v in (STRIKE, *eng.competitors(m)):
            lg = eng.leg(v, m, n, "taker")
            f = lg.quote.funding if lg.quote else None
            fills = lg.fills
            rate = lg.rate_per_hour
            venues[v] = VenueQuote(
                native_symbol=lg.native_symbol,
                mark_price_usd=lg.mark,
                index_price_usd=lg.quote.index_price if lg.quote else None,
                funding_rate_per_interval=f.rate_per_interval if f else None,
                funding_interval_hours=f.interval_hours if f else None,
                funding_rate_per_hour=rate,
                funding_paid_by_long_usd=None if rate is None else rate * n * h,
                funding_apr=calc.annualized_apr(rate),
                volume_24h_usd=lg.quote.volume_24h_usd if lg.quote else None,
                avg_buy_fill_price_usd=fills.buy_avg if fills else None,
                avg_sell_fill_price_usd=fills.sell_avg if fills else None,
                buy_slippage_bps=_bps(fills.buy_slippage_frac if fills else None),
                sell_slippage_bps=_bps(fills.sell_slippage_frac if fills else None),
                liquidity_ok=None if fills is None else fills.sufficient,
                source_ts_ms=lg.source_ts_ms,
                source_ts_iso=iso(lg.source_ts_ms),
                book_source_ts_ms=lg.book_ts_ms,
                stale=lg.stale,
                hl_fee_unverified=m.fee_unverified(v),
            )
        markets.append(
            MarketSnapshot(
                symbol=m.strike,
                asset_class=m.asset_class,
                is_tradfi=ctx.state.tradfi.get(m.strike, m.asset_class != "crypto"),
                ticker_only=m.ticker_only,
                strike_only=m.strike in strike_only,
                market_open=is_open,
                market_closed_reason=reason,
                note=m.note,
                trade_url=trade_url(ctx),
                venues=venues,
            )
        )
    return SnapshotResponse(**envelope(ctx, n, h, "taker"), markets=markets)


def _bps(frac: float | None) -> float | None:
    return None if frac is None else round(frac * 1e4, 4)


@router.get(
    "/api/v1/opportunities",
    response_model=OpportunitiesResponse,
    tags=["v1"],
    summary="Two-leg funding opportunities ranked by estimated net USD after costs",
)
def opportunities(
    request: Request,
    min_net_usd: Annotated[
        float | None, Query(description="Only ranked rows with net_usd >= this value.")
    ] = None,
    notional: Notional = None,
    hours: Hours = None,
    fee_mode: Mode = "taker",
    direction: Annotated[
        Direction | None,
        Query(description="long_strike_short_other or long_other_short_strike."),
    ] = None,
    symbol: Annotated[
        str | None, Query(description="e.g. BTC-USD or BTC", examples=["BTC-USD"])
    ] = None,
    include_unverified: Annotated[
        bool, Query(description="Rank ticker-only matches (CXMT, DRAM, ...) too.")
    ] = False,
) -> OpportunitiesResponse:
    return opportunities_data(
        ctx_of(request),
        min_net_usd,
        notional,
        hours,
        fee_mode,
        direction,
        symbol,
        include_unverified,
    )


def opportunities_data(
    ctx: Context,
    min_net_usd: float | None,
    notional: float | None,
    hours: float | None,
    fee_mode: str,
    direction: str | None,
    symbol: str | None,
    include_unverified: bool,
) -> OpportunitiesResponse:
    """Shared by GET /api/v1/opportunities and the MCP get_opportunities tool."""
    n = notional or ctx.settings.default_notional
    h = hours or ctx.settings.default_hours
    sym = norm_symbol(symbol, ctx) if symbol else None
    ranked, excluded = ctx.engine.opportunities(
        n, h, fee_mode, sym, direction, min_net_usd, include_unverified
    )
    maker = fee_mode == "maker"
    return OpportunitiesResponse(
        **envelope(ctx, n, h, fee_mode),
        opportunities=[to_opportunity(ctx, r, n, maker) for r in ranked],
        unranked=[to_opportunity(ctx, r, n, maker) for r in excluded],
        count=len(ranked),
    )


@router.get(
    "/api/v1/holding-cost",
    response_model=HoldingCostResponse,
    tags=["v1"],
    summary="Cheapest venue to hold one long or short position",
)
def holding_cost(
    request: Request,
    symbol: Annotated[str, Query(description="e.g. BTC-USD or BTC", examples=["BTC-USD"])],
    side: Annotated[Side, Query(description="long or short")],
    notional: Notional = None,
    hours: Hours = None,
    fee_mode: Mode = "taker",
) -> HoldingCostResponse:
    return holding_cost_data(ctx_of(request), symbol, side, notional, hours, fee_mode)


def holding_cost_data(
    ctx: Context,
    symbol: str,
    side: str,
    notional: float | None,
    hours: float | None,
    fee_mode: str,
) -> HoldingCostResponse:
    """Shared by GET /api/v1/holding-cost and the MCP get_holding_cost tool."""
    n = notional or ctx.settings.default_notional
    h = hours or ctx.settings.default_hours
    sym = norm_symbol(symbol, ctx)
    res = ctx.engine.holding_cost(ctx.engine.markets[sym], side, n, h, fee_mode)
    rows = []
    for vc in res.venues:
        lg, c = vc.leg, vc.cost
        f = lg.quote.funding if lg.quote else None
        rows.append(
            VenueHoldingCost(
                venue=vc.venue,
                native_symbol=lg.native_symbol,
                funding_rate_per_interval=f.rate_per_interval if f else None,
                funding_interval_hours=f.interval_hours if f else None,
                funding_rate_per_hour=lg.rate_per_hour,
                fee_rate=lg.fee_rate,
                entry_fee_usd=c.entry_fee_usd,
                exit_fee_usd=c.exit_fee_usd,
                entry_slippage_usd=c.entry_slippage_usd,
                exit_slippage_usd=c.exit_slippage_usd,
                funding_cost_usd=c.funding_cost_usd,
                total_cost_usd=c.total_cost_usd,
                liquidity_ok=c.liquidity_ok,
                stale=lg.stale,
                ranked=vc.rankable,
                exclusion_reason=vc.exclusion,
                confidence=vc.confidence,  # type: ignore[arg-type]
                confidence_reasons=vc.confidence_reasons,
                source_ts_ms=lg.source_ts_ms,
                source_ts_iso=iso(lg.source_ts_ms),
                hl_fee_unverified=res.market.fee_unverified(vc.venue),
            )
        )
    rows.sort(
        key=lambda r: (not r.ranked, r.total_cost_usd if r.total_cost_usd is not None else 1e18)
    )
    return HoldingCostResponse(
        **envelope(ctx, n, h, fee_mode),
        symbol=sym,
        side=side,
        venues_ranked=rows,
        best_venue=res.best[0] if res.best else None,
        best_total_cost_usd=res.best[1] if res.best else None,
        worst_venue=res.worst[0] if res.worst else None,
        worst_total_cost_usd=res.worst[1] if res.worst else None,
        best_vs_worst_gap_usd=res.gap_usd,
        market_open=res.market_open,
        market_closed_reason=res.closed_reason,
        ticker_only=res.market.ticker_only,
    )


SwitchFrom = Annotated[
    Literal["hyperliquid", "okx"],
    Query(description="Venue the position is on today: hyperliquid or okx."),
]
SWITCH_DEFAULT_HOURS = 336.0  # 2 weeks


def period_label(hours: float) -> str:
    """Plain-language period: 1 week, 2 weeks, 1 month, 3 days, else 36 hours."""
    if abs(hours - 720) < 1e-9:
        return "1 month"
    for unit_h, name in ((168, "week"), (24, "day")):
        n = hours / unit_h
        if n >= 1 and abs(n - round(n)) < 1e-9:
            return f"{round(n)} {name}{'' if round(n) == 1 else 's'}"
    return f"{hours:g} hours"


def _usd(x: float) -> str:
    return f"${abs(x):,.0f}" if abs(x) >= 100 else f"${abs(x):,.2f}"


def switch_summary(venue: str, saving: float | None, label: str) -> str:
    name = report.VENUE_LABEL.get(venue, venue)
    if saving is None:
        return "Not enough data to compare staying with moving (missing funding or thin book)."
    if abs(saving) < 0.005:
        return f"Staying on {name} and moving to Strike cost about the same over {label}."
    if saving > 0:
        return f"Moving to Strike is estimated to save about {_usd(saving)} over {label}."
    return f"Staying on {name} is cheaper right now, by about {_usd(saving)} over {label}."


def strike_cheaper_share(ctx: Context, symbol: str, side: str, venue: str):
    """30-day share of hours Strike was the cheaper venue for this side vs `venue` (report)."""
    rep = ctx.report()
    for m in rep.get("markets", []):
        if m["symbol"] != symbol:
            continue
        for v in m.get("view_a", []):
            if v["other_venue"] == venue:
                key = "long_strike_cheaper_pct" if side == "long" else "short_strike_cheaper_pct"
                return v.get(key), v.get("hours_compared")
    return None, None


def switch_data(
    ctx: Context,
    symbol: str,
    side: str,
    notional: float | None,
    from_venue: str,
    hours: float | None,
    fee_mode: str,
) -> SwitchResponse:
    """Shared by GET /api/v1/switch and the MCP get_switch_estimate tool."""
    n = notional or ctx.settings.default_notional
    h = hours or SWITCH_DEFAULT_HOURS
    sym = norm_symbol(symbol, ctx)
    eng = ctx.engine
    m = eng.markets[sym]
    if from_venue not in eng.competitors(m):
        raise HTTPException(
            400, f"{sym} is not available on {from_venue} here. See /api/v1/snapshot for venues."
        )
    res = eng.switch(m, side, n, h, fee_mode, from_venue)
    c = res.cost
    label = period_label(h)
    name = report.VENUE_LABEL.get(from_venue, from_venue)
    cheaper = None if c.saving_usd is None else c.saving_usd < 0
    stay_items = [
        LineItem(label=f"Funding on {name} over {label}", usd=_neg(c.stay_funding_usd)),
        LineItem(label=f"Close on {name} at the end (fee)", usd=-c.stay_exit_fee_usd),
        LineItem(label=f"Close on {name} at the end (slippage)", usd=-c.stay_exit_slippage_usd),
        LineItem(label="Cost of staying", usd=_neg(c.stay_total_cost_usd)),
    ]
    switch_items = [
        LineItem(label=f"Close on {name} now (fee)", usd=-c.close_fee_usd),
        LineItem(label=f"Close on {name} now (slippage)", usd=-c.close_slippage_usd),
        LineItem(label="Open on Strike now (fee)", usd=-c.open_fee_usd),
        LineItem(label="Open on Strike now (slippage)", usd=-c.open_slippage_usd),
        LineItem(label=f"Funding on Strike over {label}", usd=_neg(c.strike_funding_usd)),
        LineItem(label="Close on Strike at the end (fee)", usd=-c.strike_exit_fee_usd),
        LineItem(label="Close on Strike at the end (slippage)", usd=-c.strike_exit_slippage_usd),
        LineItem(label="Cost of moving to Strike", usd=_neg(c.switch_total_cost_usd)),
    ]
    share, compared = strike_cheaper_share(ctx, sym, side, from_venue)
    p = ctx.backfiller.progress if ctx.backfiller else None
    return SwitchResponse(
        **envelope(ctx, n, h, fee_mode),
        symbol=sym,
        side=side,  # type: ignore[arg-type]
        from_venue=from_venue,
        notional_usd=n,
        hours=h,
        period_label=label,
        summary=switch_summary(from_venue, c.saving_usd, label),
        staying_is_cheaper=cheaper,
        stay_cost_usd=c.stay_total_cost_usd,
        switch_cost_usd=c.switch_total_cost_usd,
        estimated_saving_usd=c.saving_usd,
        switching_cost_upfront_usd=c.upfront_cost_usd,
        funding_advantage_usd_per_hour=c.funding_advantage_per_hour_usd,
        break_even_hours=c.break_even_hours,
        stay_line_items=stay_items,
        switch_line_items=switch_items,
        from_leg=leg_out(res.current_leg),
        strike_leg=leg_out(res.strike_leg),
        confidence=res.confidence,  # type: ignore[arg-type]
        confidence_reasons=res.confidence_reasons,
        stale=res.stale,
        stale_venues=res.stale_venues,
        liquidity_ok=c.liquidity_ok,
        market_open=res.market_open,
        market_closed_reason=res.closed_reason,
        ticker_only=m.ticker_only,
        hl_fee_unverified=bool(res.fee_unverified_venues),
        strike_cheaper_share_30d=share,
        strike_cheaper_hours_compared_30d=compared,
        history_loading=bool(p and p.loading),
        trade_url=trade_url(ctx),
        note=m.note,
    )


def _neg(x: float | None) -> float | None:
    """Cost (positive reduces the result) to a line-item contribution (negative costs)."""
    return None if x is None else -x


@router.get(
    "/api/v1/switch",
    response_model=SwitchResponse,
    tags=["v1"],
    summary="Should I move my position to Strike? Stay versus switch, after costs",
)
def switch(
    request: Request,
    symbol: Annotated[str, Query(description="e.g. ETH-USD or ETH", examples=["ETH-USD"])],
    side: Annotated[Side, Query(description="The side you hold: long or short")],
    from_venue: SwitchFrom,
    notional: Notional = None,
    hours: Annotated[
        float | None,
        Query(
            gt=0,
            le=8760,
            description="How long you would keep the position, in hours (default 336 = 2 weeks).",
            examples=[336],
        ),
    ] = None,
    fee_mode: Mode = "taker",
) -> SwitchResponse:
    return switch_data(ctx_of(request), symbol, side, notional, from_venue, hours, fee_mode)


def backfill_out(ctx: Context) -> BackfillOut:
    p = ctx.backfiller.progress if ctx.backfiller else None
    if p is None:
        return BackfillOut(total=0, done=0, failed=0, skipped=0, running=False)
    return BackfillOut(
        total=p.total, done=p.done, failed=p.failed, skipped=p.skipped, running=p.running
    )


def history_loading(ctx: Context) -> bool:
    return bool(ctx.backfiller and ctx.backfiller.progress.loading)


@router.get(
    "/api/v1/report",
    response_model=ReportResponse,
    tags=["v1"],
    summary="30-day historical report: how often funding favored each direction",
)
def get_report(request: Request) -> ReportResponse:
    return report_data(ctx_of(request))


def report_data(ctx: Context, symbol: str | None = None) -> ReportResponse:
    """Shared by GET /api/v1/report and the MCP get_report tool.

    With `symbol`, only that market's table is returned; the summary, key findings and
    assumptions stay global.
    """
    n = 10_000.0
    rep = ctx.report()
    markets = rep["markets"]
    if symbol:
        sym = norm_symbol(symbol, ctx)
        markets = [m for m in markets if m["symbol"] == sym]
    return ReportResponse(
        **envelope(ctx, n, 24, "taker"),
        history_loading=history_loading(ctx),
        backfill=backfill_out(ctx),
        window_days=rep["window_days"],
        window_start_ms=rep["window_start_ms"],
        window_end_ms=rep["window_end_ms"],
        notional_usd=rep["notional_usd"],
        headline=rep["headline"],
        key_findings=rep["key_findings"],
        summary=rep["summary"],
        slippage_note=rep["slippage_note"],
        report_assumptions=rep["assumptions"],
        markets=markets,
    )


LIQUIDITY_NOTES = [
    "borrowed_share = Borrow & Earn debt / SLP TVL. Credit draws can only be deposited into SLP, "
    "so a high share means much of the vault is borrowed capital that can leave quickly.",
    "vault_apr is Strike's published SLP apr (its formula is not documented; no upstream "
    "history, so the series starts when this service started recording). derived_apr is our own "
    "figure from SLP share price over the APR window (30 days by default, simple "
    "annualization) and can differ. volume_to_tvl uses Strike's 24h perps volume.",
    "Any input that is unavailable makes the metric null (n/a); ratios are never computed from "
    "partial data. Debt history only exists since debt_history_started_ms, so debt trends are "
    "null until about 7 days after that.",
    "Information only, not a prediction: a flag is a risk indicator, not advice.",
]


@router.get(
    "/api/v1/liquidity-quality",
    response_model=LiquidityQualityResponse,
    tags=["v1"],
    summary="Strike liquidity quality: how much of SLP depth is borrowed capital",
)
def get_liquidity_quality(request: Request) -> LiquidityQualityResponse:
    return liquidity_quality_data(ctx_of(request))


def liquidity_quality_data(ctx: Context) -> LiquidityQualityResponse:
    """Shared by GET /api/v1/liquidity-quality and the MCP get_liquidity_quality tool."""
    out = ctx.liquidity()
    return LiquidityQualityResponse(
        **envelope(ctx, ctx.settings.default_notional, ctx.settings.default_hours, "taker"),
        current=out["current"],
        trend_7d=out["trend_7d"],
        flag=out["flag"],
        debt_history_started_ms=out["debt_history_started_ms"],
        series_7d=out["series_7d"],
        series_30d=out["series_30d"],
        notes=LIQUIDITY_NOTES,
    )


@router.get(
    "/api/v1/history",
    response_model=HistoryResponse,
    tags=["v1"],
    summary="Hourly-normalized funding history per venue",
)
def history(
    request: Request,
    symbol: Annotated[str, Query(description="e.g. BTC-USD", examples=["BTC-USD"])],
    days: Annotated[int, Query(ge=1, le=30, description="1 to 30 days")] = 30,
) -> HistoryResponse:
    ctx = ctx_of(request)
    sym = norm_symbol(symbol, ctx)
    series = report.funding_history_hourly(ctx.store, ctx.engine.markets, sym, ctx.now_ms(), days)
    cum = report.cumulative_for_symbol(
        ctx.store,
        ctx.settings,
        ctx.engine.markets[sym],
        ctx.now_ms(),
        days,
        slippage=ctx.engine.report_slippage(10_000.0),
    )
    return HistoryResponse(
        **envelope(ctx, ctx.settings.default_notional, ctx.settings.default_hours, "taker"),
        symbol=sym,
        days=days,
        venues_history={
            v: [HistoryPoint(ts_ms=t, funding_rate_per_hour=r) for t, r in pts]
            for v, pts in series.items()
        },
        history_loading=history_loading(ctx),
        cumulative_net_usd_on_10k={
            other: {
                d: [CumPoint(ts_ms=t, cumulative_net_usd=v) for t, v in pts]
                for d, pts in dirs.items()
            }
            for other, dirs in cum.items()
        },
    )


@router.get(
    "/healthz",
    response_model=HealthResponse,
    tags=["ops"],
    summary="Per-venue status, last error, freshness and upstream request counts",
)
def healthz(request: Request) -> HealthResponse:
    ctx = ctx_of(request)
    now = ctx.now_ms()
    venues: dict[str, VenueHealth] = {}
    for v in ALL_VENUES:
        enabled = v in ctx.settings.enabled_venues
        ts, stale = ctx.engine.venue_freshness(v)
        client = ctx.clients.get(v)
        degraded = bool(client and client.degraded)
        if not enabled:
            status = "disabled"
        elif degraded:
            status = "degraded"
        elif ts is None:
            status = "error" if ctx.state.last_error.get(v) else "starting"
        elif stale:
            status = "stale"
        else:
            status = "ok"
        counts = client.request_counts() if client else {}
        venues[v] = VenueHealth(
            venue=v,
            enabled=enabled,
            status=status,  # type: ignore[arg-type]
            stale=stale,
            degraded=degraded,
            last_success_ms=ctx.state.last_ok_ms.get(v),
            last_success_iso=iso(ctx.state.last_ok_ms.get(v)),
            freshness_seconds=None if ts is None else round((now - ts) / 1000, 1),
            last_error=ctx.state.last_error.get(v) or (client.last_error if client else None),
            last_error_ms=ctx.state.last_error_ms.get(v),
            upstream_requests_last_minute=client.requests_last_minute() if client else 0,
            upstream_requests_per_minute={
                iso(m * 60_000)[:16] + "Z": c
                for m, c in counts.items()  # type: ignore[index]
            },
            rate_limit_headers=dict(client.rate_headers) if client else {},
        )
    enabled_states = [h.status for h in venues.values() if h.enabled]
    if all(s == "starting" for s in enabled_states):
        overall = "starting"
    elif all(s == "ok" for s in enabled_states):
        overall = "ok"
    else:
        overall = "degraded"
    return HealthResponse(
        status=overall,  # type: ignore[arg-type]
        generated_at_ms=now,
        generated_at_iso=iso(now) or "",
        poll_cycles=ctx.state.cycles,
        last_cycle_ms=ctx.state.last_cycle_ms,
        venues=venues,
        history=backfill_out(ctx) if ctx.backfiller else None,
        history_loading=history_loading(ctx),
        snapshots_stored=ctx.store.count_snapshots(),
        inbound_rate_limit_per_minute=ctx.limiter.limit if ctx.limiter else 0,
    )


@router.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
def llms(request: Request) -> PlainTextResponse:
    base = str(request.base_url).rstrip("/")
    return PlainTextResponse(llms_text(base), media_type="text/plain; charset=utf-8")
