"""Pydantic response models. Field names carry units; null means missing, never 0."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"
DISCLAIMER = (
    "Estimates only, not financial advice. Funding, prices and liquidity change; "
    "results are not guaranteed."
)

FeeMode = Literal["taker", "maker"]
Side = Literal["long", "short"]
Direction = Literal["long_strike_short_other", "long_other_short_strike"]
Confidence = Literal["High", "Medium", "Low"]


class ErrorResponse(BaseModel):
    error: str = Field(description="Machine-readable error code.", examples=["rate_limited"])
    detail: str = Field(description="Human-readable explanation.", examples=["60 requests/min"])


class VenueStatus(BaseModel):
    venue: str = Field(examples=["strike"])
    enabled: bool = Field(description="Venue is enabled via ENABLED_VENUES.")
    stale: bool = Field(
        description="True when the newest source timestamp is older than 5 minutes or missing. "
        "Stale venues are excluded from rankings."
    )
    degraded: bool = Field(description="Upstream is rate limiting us; backing off.")
    source_ts_ms: int | None = Field(description="Newest source timestamp, UTC epoch ms.")
    source_ts_iso: str | None = Field(description="Same as source_ts_ms, ISO 8601 UTC.")
    age_seconds: float | None = Field(description="Seconds since source_ts_ms.")
    last_error: str | None = Field(default=None, description="Most recent upstream error.")


class Assumptions(BaseModel):
    notional_usd: float = Field(examples=[10000])
    hours: float = Field(description="Holding period in hours.", examples=[24])
    fee_mode: FeeMode
    fee_rates: dict[str, float] = Field(
        description="Fee rate per venue as a fraction of notional for the chosen fee_mode. "
        "Negative is a rebate.",
        examples=[{"strike": 0.0005, "hyperliquid": 0.00045, "okx": 0.0005}],
    )
    preset_hours: dict[str, float] = Field(examples=[{"1d": 24, "1w": 168, "1m": 720}])
    slippage_model: str
    funding_model: str
    hl_hip3_fee_multiplier: float = Field(
        description="Scales Hyperliquid fees on HIP-3 dex markets (deployer fee not exposed)."
    )
    notes: list[str]


class Envelope(BaseModel):
    schema_version: str = Field(default=SCHEMA_VERSION, examples=[SCHEMA_VERSION])
    generated_at_ms: int = Field(description="UTC epoch ms.")
    generated_at_iso: str
    venues: dict[str, VenueStatus]
    assumptions: Assumptions
    disclaimer: str = DISCLAIMER


# --- snapshot ---------------------------------------------------------------
class VenueQuote(BaseModel):
    native_symbol: str = Field(examples=["BTC-USDT-SWAP"])
    mark_price_usd: float | None
    index_price_usd: float | None
    funding_rate_per_interval: float | None = Field(
        description="Raw rate for one funding interval, fraction. Positive: longs pay shorts."
    )
    funding_interval_hours: float | None
    funding_rate_per_hour: float | None = Field(
        description="Rate normalized to hourly (per interval / interval hours)."
    )
    funding_paid_by_long_usd: float | None = Field(
        description="Funding over assumptions.hours on assumptions.notional_usd. Positive: a long "
        "pays, a short receives. Negative: a long receives."
    )
    funding_apr: float | None = Field(
        description="Simple annualized rate (hourly x 24 x 365), fraction. Secondary figure."
    )
    volume_24h_usd: float | None
    avg_buy_fill_price_usd: float | None
    avg_sell_fill_price_usd: float | None
    buy_slippage_bps: float | None
    sell_slippage_bps: float | None
    liquidity_ok: bool | None = Field(
        description="Book absorbs notional_usd on both sides. null when no book."
    )
    source_ts_ms: int | None
    source_ts_iso: str | None
    book_source_ts_ms: int | None
    stale: bool
    hl_fee_unverified: bool = Field(description="HIP-3 deployer fee not exposed by the API.")


class MarketSnapshot(BaseModel):
    symbol: str = Field(examples=["BTC-USD"])
    asset_class: str = Field(examples=["crypto"])
    is_tradfi: bool = Field(description="Stock, index or commodity market.")
    ticker_only: bool = Field(description="Matched to other venues by ticker only.")
    strike_only: bool = Field(description="No competitor mapping; Strike-only section.")
    market_open: bool
    market_closed_reason: str | None
    note: str
    trade_url: str
    venues: dict[str, VenueQuote]


class SnapshotResponse(Envelope):
    markets: list[MarketSnapshot]


# --- opportunities --------------------------------------------------------------
class LineItem(BaseModel):
    label: str
    usd: float | None = Field(description="Contribution to net (positive adds, negative costs).")


class LegOut(BaseModel):
    venue: str
    native_symbol: str
    funding_rate_per_interval: float | None
    funding_interval_hours: float | None
    funding_rate_per_hour: float | None
    mark_price_usd: float | None
    fee_rate: float
    stale: bool
    source_ts_ms: int | None
    source_ts_iso: str | None
    liquidity_ok: bool | None


class Opportunity(BaseModel):
    rank: int | None = Field(description="1 = best estimated net. null when not ranked.")
    symbol: str
    asset_class: str
    direction: Direction
    long_venue: str
    short_venue: str
    net_usd: float | None = Field(
        description="Estimated net over assumptions.hours: funding both legs minus fees and "
        "slippage on four fills. An estimate, not profit."
    )
    net_usd_by_period: dict[str, float | None] = Field(
        description="Estimated net for 1d, 1w, 1m assuming current funding persists; round-trip "
        "costs paid once.",
        examples=[{"1d": -17.4, "1w": 112.2, "1m": 609.0}],
    )
    carry_usd_per_hour: float | None = Field(description="Net funding carry per hour.")
    funding_net_usd: float | None
    fees_usd: float
    slippage_usd: float
    round_trip_cost_usd: float
    break_even_hours: float | None = Field(
        description="Round-trip costs / net hourly carry. null when carry is not positive."
    )
    basis_bps: float | None = Field(
        description="(mark long venue - mark short venue) / mean mark x 10000. Basis and funding "
        "can change and closing prices may differ."
    )
    confidence: Confidence
    confidence_reasons: list[str]
    stale: bool = Field(description="Any leg's data older than 5 minutes.")
    stale_venues: list[str]
    liquidity_ok: bool
    ticker_only: bool
    hl_fee_unverified: bool
    market_open: bool
    market_closed_reason: str | None
    exclusion_reason: str | None = Field(description="Why this row is not ranked, if so.")
    long_leg: LegOut
    short_leg: LegOut
    line_items: list[LineItem]
    trade_url: str
    note: str


class OpportunitiesResponse(Envelope):
    opportunities: list[Opportunity] = Field(description="Ranked rows, best first.")
    unranked: list[Opportunity] = Field(
        description="Rows excluded from ranking (stale, thin, ticker-only, missing data)."
    )
    count: int


# --- holding cost -----------------------------------------------------------------
class VenueHoldingCost(BaseModel):
    venue: str
    native_symbol: str
    funding_rate_per_interval: float | None
    funding_interval_hours: float | None
    funding_rate_per_hour: float | None
    fee_rate: float
    entry_fee_usd: float
    exit_fee_usd: float
    entry_slippage_usd: float
    exit_slippage_usd: float
    funding_cost_usd: float | None = Field(
        description="Funding paid over the period (negative: received). null when missing."
    )
    total_cost_usd: float | None
    liquidity_ok: bool
    stale: bool
    ranked: bool
    exclusion_reason: str | None
    confidence: Confidence
    confidence_reasons: list[str]
    source_ts_ms: int | None
    source_ts_iso: str | None
    hl_fee_unverified: bool


class HoldingCostResponse(Envelope):
    symbol: str
    side: Side
    venues_ranked: list[VenueHoldingCost]
    best_venue: str | None
    best_total_cost_usd: float | None
    worst_venue: str | None
    worst_total_cost_usd: float | None
    best_vs_worst_gap_usd: float | None
    market_open: bool
    market_closed_reason: str | None
    ticker_only: bool


class SwitchResponse(Envelope):
    symbol: str
    side: Side
    from_venue: str = Field(description="Venue the position is on today: hyperliquid or okx.")
    notional_usd: float
    hours: float = Field(description="How long the position would be kept, in hours.")
    period_label: str = Field(
        description="Plain-language period, e.g. 2 weeks.", examples=["2 weeks"]
    )
    summary: str = Field(description="One plain sentence: which choice is cheaper and by how much.")
    staying_is_cheaper: bool | None = Field(
        description="True when staying on from_venue costs less. null when data is missing."
    )
    stay_cost_usd: float | None = Field(
        description="Funding on from_venue plus its exit fee and slippage at the end. "
        "Positive is a cost, negative means you receive. null when data is missing."
    )
    switch_cost_usd: float | None = Field(
        description="Close on from_venue now, open on Strike now, funding on Strike, close on "
        "Strike at the end. Positive is a cost, negative means you receive."
    )
    estimated_saving_usd: float | None = Field(
        description="stay_cost_usd minus switch_cost_usd. Negative means staying is cheaper."
    )
    switching_cost_upfront_usd: float = Field(
        description="Paid now if you move: exit fee and slippage on from_venue plus entry fee "
        "and slippage on Strike."
    )
    funding_advantage_usd_per_hour: float | None = Field(
        description="Funding you save per hour by being on Strike (negative: Strike costs more)."
    )
    break_even_hours: float | None = Field(
        description="Hours until the estimated saving turns positive (Strike entry and exit "
        "costs divided by the hourly funding advantage). null unless the advantage is positive."
    )
    stay_line_items: list[LineItem]
    switch_line_items: list[LineItem]
    from_leg: LegOut
    strike_leg: LegOut
    confidence: Confidence
    confidence_reasons: list[str]
    stale: bool
    stale_venues: list[str]
    liquidity_ok: bool
    market_open: bool
    market_closed_reason: str | None
    ticker_only: bool
    hl_fee_unverified: bool
    strike_cheaper_share_30d: float | None = Field(
        description="Fraction (0 to 1) of the last 30 days' hours in which Strike's funding made "
        "holding this side cheaper than on from_venue (funding only, from the report). "
        "null while history is loading or missing."
    )
    strike_cheaper_hours_compared_30d: int | None
    history_loading: bool
    trade_url: str
    note: str


# --- history -----------------------------------------------------------------------
class HistoryPoint(BaseModel):
    ts_ms: int = Field(description="Start of the hour, UTC epoch ms.")
    funding_rate_per_hour: float | None = Field(description="null = no data for that hour.")


class CumPoint(BaseModel):
    ts_ms: int
    cumulative_net_usd: float = Field(
        description="Running two-leg funding carry minus round-trip taker costs on $10,000."
    )


class HistoryResponse(Envelope):
    symbol: str
    days: int
    resolution: str = "1h"
    unit: str = "fraction per hour; positive means longs pay shorts"
    venues_history: dict[str, list[HistoryPoint]]
    history_loading: bool
    cumulative_net_usd_on_10k: dict[str, dict[str, list[CumPoint]]] = Field(
        description="other venue -> direction -> running estimated net on a $10,000 two-leg "
        "position (hours with no data add nothing). Past funding does not predict future funding."
    )
    note: str = "OKX payments are spread evenly over the hours of their interval."


# --- report ---------------------------------------------------------------------------
class PairStatsOut(BaseModel):
    symbol: str
    other_venue: str
    direction: Direction
    notional_usd: float
    hours_compared: int
    pct_hours_positive: float | None
    avg_carry_usd_per_hour: float | None
    round_trip_cost_usd: float
    days_to_cover_costs: float | None
    cum_net_7d_usd: float | None
    cum_net_30d_usd: float | None
    longest_favorable_hours: int
    slippage_bps_strike: float | None
    slippage_bps_other: float | None
    low_confidence: bool
    low_confidence_reason: str | None


class ViewAStatsOut(BaseModel):
    other_venue: str
    hours_compared: int
    long_strike_cheaper_pct: float | None
    short_strike_cheaper_pct: float | None
    equal_pct: float | None


class KeyFinding(BaseModel):
    kind: Literal[
        "below_index", "above_index", "lower_funding", "higher_funding", "balanced", "none"
    ]
    title: str
    text: str = Field(description="Plain-language finding generated from the report data.")
    markets: list[str]
    details: list[str]
    caveat: str


class MarketReportOut(BaseModel):
    symbol: str
    asset_class: str
    ticker_only: bool
    notes: str
    hours_with_data: dict[str, int]
    slippage_used: dict[str, dict[str, Any]]
    pairs: list[PairStatsOut]
    view_a: list[ViewAStatsOut]
    strike_hours: int
    strike_negative_pct: float | None = Field(
        description="Share of hours Strike funding was negative (perp below index)."
    )
    strike_positive_pct: float | None


class BackfillOut(BaseModel):
    total: int
    done: int
    failed: int
    skipped: int
    running: bool


class ReportResponse(Envelope):
    history_loading: bool
    backfill: BackfillOut
    window_days: int
    window_start_ms: int | None
    window_end_ms: int | None
    notional_usd: float
    headline: str | None = Field(description="One-line takeaway built from key_findings.")
    key_findings: list[KeyFinding]
    summary: list[str]
    slippage_note: str
    report_assumptions: dict[str, Any]
    markets: list[MarketReportOut]


# --- liquidity quality ---------------------------------------------------------------------
TrendDirection = Literal["up", "down", "flat"]


class LiquidityTrendOut(BaseModel):
    direction: TrendDirection | None = Field(
        description="Change over 7 days against the thresholds in `flag.thresholds`. "
        "null until a baseline sample about 7 days old exists."
    )
    change: float | None = Field(
        description="borrowed_share: absolute change (0.03 = +3 percentage points). "
        "volume_to_tvl: relative change (0.25 = +25%)."
    )
    baseline: float | None = Field(description="Metric value 7 days ago.")
    baseline_ts_ms: int | None


class LiquidityCurrentOut(BaseModel):
    sample_ts_ms: int | None = Field(description="Sample the borrowed share was computed from.")
    borrowed_share: float | None = Field(
        description="Borrow & Earn debt / SLP TVL, as a fraction (0.44 = 44%). null = n/a.",
        examples=[0.437],
    )
    vault_apr: float | None = Field(
        description="Strike's published SLP vault apr, as a fraction (0.25 = 25%). Strike does "
        "not document its window or formula. null = n/a (never replaced by derived_apr).",
        examples=[0.255],
    )
    derived_apr: float | None = Field(
        description="Secondary: our own APR from SLP share price growth over the APR window "
        "(30 days by default), annualized (simple), as a fraction. It can differ from vault_apr. "
        "null = n/a.",
        examples=[0.375],
    )
    volume_to_tvl: float | None = Field(
        description="Strike 24h perps volume / SLP TVL. null = n/a.", examples=[10.8]
    )
    slp_tvl_usd: float | None
    debt_total_usd: float | None
    debt_by_asset: dict[str, float] | None = Field(
        description="Debt in USD per collateral asset (ADA, sUSDrf, enzoBTC, ...)."
    )
    volume_24h_usd: float | None


class LiquidityFlagOut(BaseModel):
    status: Literal["fragile", "ok", "insufficient_data"] = Field(
        description="fragile = borrowed share high AND rising over 7 days AND volume_to_tvl "
        "flat or falling. insufficient_data = an input is n/a and no condition is known false."
    )
    summary: str
    conditions: dict[str, bool | None] = Field(
        description="Each condition; null = could not be evaluated (n/a)."
    )
    thresholds: dict[str, float] = Field(description="Active thresholds (configurable).")


class LiquidityPoint(BaseModel):
    ts_ms: int
    borrowed_share: float | None
    vault_apr: float | None = Field(
        description="Strike's published apr as sampled at this time (a fraction). null before "
        "this service started recording it: Strike publishes no apr history."
    )
    derived_apr: float | None = Field(
        description="Trailing derived APR from share price over the APR window (a fraction)."
    )
    volume_to_tvl: float | None
    slp_tvl_usd: float | None
    debt_total_usd: float | None
    volume_24h_usd: float | None


class LiquidityQualityResponse(Envelope):
    current: LiquidityCurrentOut
    trend_7d: dict[str, LiquidityTrendOut] = Field(description="borrowed_share and volume_to_tvl.")
    flag: LiquidityFlagOut
    debt_history_started_ms: int | None = Field(
        description="First stored debt sample. Debt has no upstream history, so the 7d and 30d "
        "debt series and the borrowed share trend start here."
    )
    series_7d: list[LiquidityPoint] = Field(description="Hourly.")
    series_30d: list[LiquidityPoint] = Field(description="Daily (UTC midnight).")
    notes: list[str]


# --- health ------------------------------------------------------------------------------
class VenueHealth(BaseModel):
    venue: str
    enabled: bool
    status: Literal["ok", "degraded", "stale", "error", "starting", "disabled"]
    stale: bool
    degraded: bool
    last_success_ms: int | None
    last_success_iso: str | None
    freshness_seconds: float | None = Field(description="Age of the newest source timestamp.")
    last_error: str | None
    last_error_ms: int | None
    upstream_requests_last_minute: int
    upstream_requests_per_minute: dict[str, int] = Field(
        description="Upstream requests sent per UTC minute (ISO minute -> count)."
    )
    rate_limit_headers: dict[str, str]


class HealthResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    status: Literal["ok", "degraded", "starting"]
    generated_at_ms: int
    generated_at_iso: str
    poll_cycles: int
    last_cycle_ms: int | None
    venues: dict[str, VenueHealth]
    history: BackfillOut | None
    history_loading: bool
    snapshots_stored: int
    inbound_rate_limit_per_minute: int
    model_config = ConfigDict(json_schema_extra={"examples": []})
