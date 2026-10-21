"""Pure cost and funding math. No I/O.

Conventions
-----------
* Funding rates are fractions (0.0001 = 0.01%). Positive means longs pay shorts.
* Money is USD. "cost" is positive when it reduces the trader's result.
* Slippage per fill = notional * |avg_fill - mid| / mid (first order estimate).
* Maker mode: fees use the maker rate and slippage is assumed zero (resting orders
  are assumed to fill at their limit price). Fills are not guaranteed.
* basis_bps = (mark_long_venue - mark_short_venue) / mean(marks) * 10_000. A negative
  value means buying on the long venue is cheaper than selling on the short venue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import FRESH_MS, STALE_AFTER_MS
from app.models import Level

Side = str  # "long" | "short"


# --- basic helpers --------------------------------------------------------
def hourly_rate(rate_per_interval: float | None, interval_hours: float) -> float | None:
    """Normalize a per-interval funding rate to hourly. None stays None (missing)."""
    if rate_per_interval is None:
        return None
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")
    return rate_per_interval / interval_hours


def annualized_apr(rate_per_hour: float | None) -> float | None:
    """Simple (non compounded) APR from an hourly rate."""
    return None if rate_per_hour is None else rate_per_hour * 24 * 365


def funding_pnl_usd(
    side: Side, rate_per_hour: float | None, notional: float, hours: float
) -> float | None:
    """Funding received (+) or paid (-) by a position held `hours` at a constant rate."""
    if rate_per_hour is None:
        return None
    amount = rate_per_hour * notional * hours
    if side == "long":
        return -amount
    if side == "short":
        return amount
    raise ValueError(f"side must be long or short, got {side!r}")


def fee_usd(notional: float, fee_rate: float) -> float:
    """Fee for one fill. Negative fee_rate gives a negative cost (rebate)."""
    return notional * fee_rate


# --- order book -----------------------------------------------------------
def walk_book(levels: tuple[Level, ...] | list[Level], notional: float) -> float | None:
    """Average fill price for spending `notional` USD through `levels` (best first).

    Returns None when the book cannot absorb the notional (insufficient liquidity).
    """
    if notional <= 0:
        raise ValueError("notional must be positive")
    remaining = notional
    base = 0.0
    for price, qty in levels:
        if price <= 0 or qty <= 0:
            continue
        level_usd = price * qty
        take = min(remaining, level_usd)
        base += take / price
        remaining -= take
        if remaining <= 1e-9:
            return notional / base
    return None


def slippage_fraction(avg_price: float, mid: float, side: str) -> float:
    """Adverse slippage vs mid as a fraction. side is the taker action: buy or sell."""
    if side == "buy":
        return (avg_price - mid) / mid
    if side == "sell":
        return (mid - avg_price) / mid
    raise ValueError(f"side must be buy or sell, got {side!r}")


@dataclass(frozen=True)
class FillEstimate:
    buy_avg: float | None
    sell_avg: float | None
    buy_slippage_frac: float | None
    sell_slippage_frac: float | None

    @property
    def sufficient(self) -> bool:
        return self.buy_avg is not None and self.sell_avg is not None


def estimate_fills(
    bids: tuple[Level, ...], asks: tuple[Level, ...], notional: float
) -> FillEstimate:
    """Average buy/sell fills for `notional` and slippage vs the book mid."""
    if not bids or not asks:
        return FillEstimate(None, None, None, None)
    mid = (bids[0][0] + asks[0][0]) / 2
    buy = walk_book(asks, notional)
    sell = walk_book(bids, notional)
    return FillEstimate(
        buy,
        sell,
        None if buy is None else slippage_fraction(buy, mid, "buy"),
        None if sell is None else slippage_fraction(sell, mid, "sell"),
    )


def okx_contracts_to_base(contracts: float, ct_val: float, ct_mult: float = 1.0) -> float:
    """OKX sizes are in contracts: base units = contracts * ctVal * ctMult."""
    return contracts * ct_val * ct_mult


# --- View A: holding cost of one position ---------------------------------
@dataclass(frozen=True)
class HoldingCost:
    side: str
    notional: float
    hours: float
    entry_fee_usd: float
    exit_fee_usd: float
    entry_slippage_usd: float
    exit_slippage_usd: float
    funding_cost_usd: float | None  # positive = paid, negative = received; None = no data
    total_cost_usd: float | None  # None when funding or liquidity is missing
    liquidity_ok: bool


def holding_cost(
    side: Side,
    notional: float,
    hours: float,
    fee_rate: float,
    fills: FillEstimate | None,
    rate_per_hour: float | None,
    maker: bool = False,
) -> HoldingCost:
    """Cost of opening a position and closing it after `hours` (View A).

    long: entry = buy, exit = sell. short: entry = sell, exit = buy.
    """
    fee = fee_usd(notional, fee_rate)
    entry_slip = exit_slip = 0.0
    liquidity_ok = True
    if not maker:
        if fills is None or not fills.sufficient:
            liquidity_ok = False
        else:
            assert fills.buy_slippage_frac is not None and fills.sell_slippage_frac is not None
            buy_s, sell_s = fills.buy_slippage_frac, fills.sell_slippage_frac
            entry_frac, exit_frac = (buy_s, sell_s) if side == "long" else (sell_s, buy_s)
            entry_slip, exit_slip = notional * entry_frac, notional * exit_frac
    pnl = funding_pnl_usd(side, rate_per_hour, notional, hours)
    funding_cost = None if pnl is None else -pnl
    total = None
    if liquidity_ok and funding_cost is not None:
        total = 2 * fee + entry_slip + exit_slip + funding_cost
    return HoldingCost(
        side, notional, hours, fee, fee, entry_slip, exit_slip, funding_cost, total, liquidity_ok
    )


# --- View B: two-leg funding strategy -------------------------------------
@dataclass(frozen=True)
class TwoLeg:
    notional: float
    hours: float
    long_venue: str
    short_venue: str
    funding_long_usd: float | None  # funding on the long leg (negative = paid)
    funding_short_usd: float | None
    funding_net_usd: float | None
    carry_per_hour_usd: float | None
    fees_usd: float  # four fills
    slippage_usd: float  # four fills
    net_usd: float | None
    break_even_hours: float | None
    basis_bps: float | None
    liquidity_ok: bool


def basis_bps(mark_long_venue: float | None, mark_short_venue: float | None) -> float | None:
    if mark_long_venue is None or mark_short_venue is None:
        return None
    mean = (mark_long_venue + mark_short_venue) / 2
    return (mark_long_venue - mark_short_venue) / mean * 10_000


def two_leg(
    notional: float,
    hours: float,
    long_venue: str,
    short_venue: str,
    long_rate_per_hour: float | None,
    short_rate_per_hour: float | None,
    long_fee_rate: float,
    short_fee_rate: float,
    long_fills: FillEstimate | None,
    short_fills: FillEstimate | None,
    long_mark: float | None = None,
    short_mark: float | None = None,
    maker: bool = False,
) -> TwoLeg:
    """Long on `long_venue`, short on `short_venue`, equal notional (View B).

    net = funding on both legs - fees on four fills - slippage on four fills.
    break_even_hours = round-trip costs / net hourly carry, only when carry > 0.
    """
    fees = 2 * fee_usd(notional, long_fee_rate) + 2 * fee_usd(notional, short_fee_rate)
    slip = 0.0
    liquidity_ok = True
    if not maker:
        for fills in (long_fills, short_fills):
            if fills is None or not fills.sufficient:
                liquidity_ok = False
                continue
            assert fills.buy_slippage_frac is not None and fills.sell_slippage_frac is not None
            slip += notional * (fills.buy_slippage_frac + fills.sell_slippage_frac)
    f_long = funding_pnl_usd("long", long_rate_per_hour, notional, hours)
    f_short = funding_pnl_usd("short", short_rate_per_hour, notional, hours)
    funding_net = None if f_long is None or f_short is None else f_long + f_short
    carry_hr = (
        None
        if long_rate_per_hour is None or short_rate_per_hour is None
        else notional * (short_rate_per_hour - long_rate_per_hour)
    )
    net = None
    if liquidity_ok and funding_net is not None:
        net = funding_net - fees - slip
    return TwoLeg(
        notional,
        hours,
        long_venue,
        short_venue,
        f_long,
        f_short,
        funding_net,
        carry_hr,
        fees,
        slip,
        net,
        break_even_hours(fees + slip, carry_hr) if liquidity_ok else None,
        basis_bps(long_mark, short_mark),
        liquidity_ok,
    )


def break_even_hours(round_trip_cost_usd: float, carry_per_hour_usd: float | None) -> float | None:
    """Hours of positive carry needed to cover round-trip costs. None if carry <= 0."""
    if carry_per_hour_usd is None or carry_per_hour_usd <= 0:
        return None
    return max(round_trip_cost_usd, 0.0) / carry_per_hour_usd


# --- period presets -------------------------------------------------------
def scale_to_hours(per_hour_value: float, hours: float) -> float:
    return per_hour_value * hours


def preset_nets(
    carry_per_hour_usd: float | None, round_trip_cost_usd: float, presets: dict[str, float]
) -> dict[str, float | None]:
    """Net for each preset period assuming the current carry persists.

    Round-trip costs are paid once regardless of the holding period.
    """
    if carry_per_hour_usd is None:
        return {k: None for k in presets}
    return {k: carry_per_hour_usd * h - round_trip_cost_usd for k, h in presets.items()}


# --- freshness, confidence, ranking ---------------------------------------
def is_stale(source_ts_ms: int | None, now_ms: int, limit_ms: int = STALE_AFTER_MS) -> bool:
    return source_ts_ms is None or now_ms - source_ts_ms > limit_ms


def confidence(
    source_ts_ms: list[int | None],
    now_ms: int,
    depth_ok: bool,
    sign_consistency: float | None,
) -> str:
    """High / Medium / Low.

    Inputs: all legs fresh (< 2 min), book depth sufficient at size, and the share of
    the last 24h of history in which the funding carry had the same sign as now
    (None when history is missing).
    High: fresh, depth ok, consistency >= 0.8. Low: stale legs, thin book or
    consistency < 0.5 (or unknown). Otherwise Medium.
    """
    fresh = all(not is_stale(t, now_ms, FRESH_MS) for t in source_ts_ms)
    any_stale = any(is_stale(t, now_ms, STALE_AFTER_MS) for t in source_ts_ms)
    if any_stale or not depth_ok or sign_consistency is None or sign_consistency < 0.5:
        return "Low"
    if fresh and sign_consistency >= 0.8:
        return "High"
    return "Medium"


def sign_consistency(rates_per_hour: list[float | None], current_sign: int) -> float | None:
    """Share of known hourly carry values with the same sign as `current_sign`."""
    known = [r for r in rates_per_hour if r is not None]
    if not known or current_sign == 0:
        return None
    same = sum(1 for r in known if (r > 0) - (r < 0) == current_sign)
    return same / len(known)


_LEVELS = ("Low", "Medium", "High")


def assess_confidence(
    source_ts_ms: list[int | None],
    now_ms: int,
    depth_ok: bool,
    sign_consistency_value: float | None,
    market_open: bool = True,
    closed_reason: str | None = None,
    ticker_only: bool = False,
    fee_unverified: bool = False,
) -> tuple[str, list[str]]:
    """Confidence level plus plain-language reasons for anything that lowered it.

    Rules on top of `confidence`: ticker-only matches are always Low; a closed
    underlying market forces Low; an unverified fee (HIP-3) lowers one level.
    """
    reasons: list[str] = []
    if any(is_stale(t, now_ms, STALE_AFTER_MS) for t in source_ts_ms):
        reasons.append("Stale data from at least one venue")
    elif any(is_stale(t, now_ms, FRESH_MS) for t in source_ts_ms):
        reasons.append("Data older than 2 minutes")
    if not depth_ok:
        reasons.append("Order book too thin at this size")
    if sign_consistency_value is None:
        reasons.append("Not enough funding history to judge consistency")
    elif sign_consistency_value < 0.8:
        reasons.append("Funding direction was not consistent over the last 24h")
    level = confidence(source_ts_ms, now_ms, depth_ok, sign_consistency_value)
    if fee_unverified:
        level = _LEVELS[max(0, _LEVELS.index(level) - 1)]
        reasons.append("HL fee unverified (HIP-3 deployer fee not exposed)")
    if not market_open:
        level = "Low"
        reasons.append(f"Underlying market closed: {closed_reason or 'outside trading hours'}")
    if ticker_only:
        level = "Low"
        reasons.append("Ticker match only: contract equivalence not verified")
    return level, reasons


@dataclass
class Opportunity:
    symbol: str
    long_venue: str
    short_venue: str
    net_usd: float | None
    stale: bool = False
    liquidity_ok: bool = True
    ticker_only: bool = False
    extra: dict = field(default_factory=dict)


def rank_opportunities(
    opps: list[Opportunity], include_unverified: bool = False
) -> list[Opportunity]:
    """Rank by estimated net dollars after costs, descending.

    Excludes stale rows, rows with insufficient liquidity and rows with no net figure.
    Ticker-only matches are excluded unless `include_unverified` is set.
    """
    ok = [
        o
        for o in opps
        if not o.stale
        and o.liquidity_ok
        and o.net_usd is not None
        and (include_unverified or not o.ticker_only)
    ]
    return sorted(ok, key=lambda o: o.net_usd, reverse=True)  # type: ignore[arg-type,return-value]


def best_worst_for_direction(
    costs_by_venue: dict[str, float | None],
) -> tuple[tuple[str, float] | None, tuple[str, float] | None, float | None]:
    """View A direction toggle: cheapest and costliest venue and the dollar gap.

    Venues with a None cost are ignored. Needs at least two venues for a gap.
    """
    known = {v: c for v, c in costs_by_venue.items() if c is not None}
    if not known:
        return None, None, None
    best = min(known.items(), key=lambda kv: kv[1])
    worst = max(known.items(), key=lambda kv: kv[1])
    gap = worst[1] - best[1] if len(known) > 1 else None
    return best, worst, gap
