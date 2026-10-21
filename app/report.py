"""Historical funding report math.

Hour convention: a funding payment stamped T with interval I hours covers the I hours
that ended at T. Each of those hours gets rate/I (so an 8h OKX rate is spread evenly
over 8 hours). Hours are identified by their start time (UTC ms, floored to the hour).
Only the last fully completed hour is used; the current hour is excluded.

Carry for a two-leg position of notional N, per hour:
  long Strike / short other:  N * (rate_other - rate_strike)
  long other / short Strike:  N * (rate_strike - rate_other)
Missing hours are never treated as zero: they are skipped, and they break
"consecutive favorable hours" streaks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.config import HYPERLIQUID, OKX, STRIKE, MarketMap, Settings
from app.db import Store
from app.models import FundingPoint

HOUR = 3_600_000
LONG_STRIKE = "long_strike_short_other"
LONG_OTHER = "long_other_short_strike"
DIRECTIONS = (LONG_STRIKE, LONG_OTHER)
VENUE_LABEL = {STRIKE: "Strike", HYPERLIQUID: "Hyperliquid", OKX: "OKX"}
EPS = 1e-12


def window_hours(now_ms: int, days: int) -> list[int]:
    """Start times of the last `days*24` fully completed hours, oldest first."""
    end = now_ms // HOUR * HOUR
    return [end - k * HOUR for k in range(days * 24, 0, -1)]


def hourly_series(points: list[FundingPoint], hours: list[int]) -> dict[int, float]:
    """Spread each payment evenly over the hours it covers. Returns hour -> rate per hour."""
    if not hours:
        return {}
    lo, hi = hours[0], hours[-1]
    out: dict[int, float] = {}
    for p in points:
        n = max(1, round(p.interval_hours))
        t_hour = p.ts_ms // HOUR * HOUR
        rph = p.rate_per_interval / p.interval_hours
        for k in range(1, n + 1):
            h = t_hour - k * HOUR
            if lo <= h <= hi:
                out[h] = rph
    return out


def carry_series(
    strike: dict[int, float], other: dict[int, float], direction: str, notional: float
) -> dict[int, float]:
    """Two-leg funding carry in USD per hour, for hours where both venues have data."""
    sign = 1 if direction == LONG_STRIKE else -1
    return {h: sign * notional * (other[h] - strike[h]) for h in strike if h in other}


def round_trip_cost_usd(
    notional: float, strike_fee: float, other_fee: float, slippage_bps_per_fill: float
) -> float:
    """Four taker fills: fees plus a flat slippage assumption per fill."""
    return notional * (2 * strike_fee + 2 * other_fee) + 4 * notional * slippage_bps_per_fill / 1e4


def longest_streak(carry: dict[int, float], hours: list[int]) -> int:
    """Longest run of consecutive hours with carry > 0. A missing hour ends the run."""
    best = run = 0
    for h in hours:
        v = carry.get(h)
        if v is not None and v > 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def cumulative_net_series(
    carry: dict[int, float], hours: list[int], cost_usd: float
) -> list[tuple[int, float]]:
    """Running carry minus round-trip costs. Starts at -cost; gaps add nothing.

    Returns (timestamp_ms, cumulative_net_usd) with one point per hour end.
    """
    if not hours:
        return []
    cum = -cost_usd
    out = [(hours[0], cum)]
    for h in hours:
        cum += carry.get(h, 0.0)
        out.append((h + HOUR, cum))
    return out


@dataclass
class PairStats:
    symbol: str
    other_venue: str
    direction: str
    notional_usd: float
    hours_compared: int
    pct_hours_positive: float | None
    avg_carry_usd_per_hour: float | None
    round_trip_cost_usd: float
    days_to_cover_costs: float | None
    cum_net_7d_usd: float | None
    cum_net_30d_usd: float | None
    longest_favorable_hours: int


@dataclass
class ViewAStats:
    other_venue: str
    hours_compared: int
    long_strike_cheaper_pct: float | None
    short_strike_cheaper_pct: float | None
    equal_pct: float | None


def pair_stats(
    symbol: str,
    other_venue: str,
    direction: str,
    strike: dict[int, float],
    other: dict[int, float],
    hours: list[int],
    notional: float,
    cost_usd: float,
) -> PairStats:
    carry = carry_series(strike, other, direction, notional)
    n = len(carry)
    last7 = set(hours[-7 * 24 :])
    c7 = [v for h, v in carry.items() if h in last7]
    if n == 0:
        return PairStats(symbol, other_venue, direction, notional, 0, None, None, cost_usd,
                         None, None, None, 0)  # fmt: skip
    avg = sum(carry.values()) / n
    return PairStats(
        symbol,
        other_venue,
        direction,
        notional,
        n,
        sum(1 for v in carry.values() if v > 0) / n,
        avg,
        cost_usd,
        cost_usd / (avg * 24) if avg > 0 else None,
        (sum(c7) - cost_usd) if c7 else None,
        sum(carry.values()) - cost_usd,
        longest_streak(carry, hours),
    )


def view_a_stats(other_venue: str, strike: dict[int, float], other: dict[int, float]) -> ViewAStats:
    """Share of hours Strike was strictly cheaper to hold (funding only).

    Long: cheaper when Strike's rate is lower. Short: cheaper when Strike's rate is
    higher (a short receives positive funding).
    """
    common = [h for h in strike if h in other]
    n = len(common)
    if n == 0:
        return ViewAStats(other_venue, 0, None, None, None)
    diffs = [strike[h] - other[h] for h in common]
    eq = sum(1 for d in diffs if abs(d) < EPS)
    return ViewAStats(
        other_venue,
        n,
        sum(1 for d in diffs if d < -EPS) / n,
        sum(1 for d in diffs if d > EPS) / n,
        eq / n,
    )


@dataclass
class MarketReport:
    symbol: str
    asset_class: str
    ticker_only: bool
    notes: str
    hours_with_data: dict[str, int]
    pairs: list[PairStats] = field(default_factory=list)
    view_a: list[ViewAStats] = field(default_factory=list)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def build_report(
    store: Store,
    settings: Settings,
    markets: list[MarketMap],
    now_ms: int,
    days: int = 30,
    notional: float = 10_000.0,
) -> dict:
    hours = window_hours(now_ms, days)
    since = (hours[0] - 8 * HOUR) if hours else 0
    reports: list[MarketReport] = []
    for m in markets:
        pts = {STRIKE: store.get_funding(STRIKE, m.strike, since)}
        if m.hl_coin:
            pts[HYPERLIQUID] = store.get_funding(HYPERLIQUID, m.strike, since)
        if m.okx_inst:
            pts[OKX] = store.get_funding(OKX, m.strike, since)
        series = {v: hourly_series(p, hours) for v, p in pts.items()}
        mr = MarketReport(
            m.strike, m.asset_class, m.ticker_only, m.note, {v: len(s) for v, s in series.items()}
        )
        for other in (HYPERLIQUID, OKX):
            if other not in series or not series[STRIKE] or not series[other]:
                continue
            cost = round_trip_cost_usd(
                notional,
                settings.fee_rate(STRIKE, "taker", m),
                settings.fee_rate(other, "taker", m),
                settings.report_slippage_bps_per_fill,
            )
            for d in DIRECTIONS:
                mr.pairs.append(
                    pair_stats(
                        m.strike, other, d, series[STRIKE], series[other], hours, notional, cost
                    )
                )
            mr.view_a.append(view_a_stats(other, series[STRIKE], series[other]))
        reports.append(mr)
    return {
        "window_days": days,
        "window_start_ms": hours[0] if hours else None,
        "window_end_ms": hours[-1] + HOUR if hours else None,
        "notional_usd": notional,
        "assumptions": assumptions(settings, notional, days),
        "summary": summarize(reports, days),
        "markets": [asdict(r) for r in reports],
    }


def assumptions(settings: Settings, notional: float, days: int) -> dict:
    return {
        "notional_usd": notional,
        "window_days": days,
        "fees_taker": {v: settings.fees[v]["taker"] for v in settings.fees},
        "slippage_bps_per_fill": settings.report_slippage_bps_per_fill,
        "fills_per_round_trip": 4,
        "hl_hip3_fee_multiplier": settings.hl_hip3_fee_multiplier,
        "notes": [
            "Past funding does not predict future funding.",
            "Round-trip costs are paid once; cumulative net adds hourly carry on top.",
            "Hours missing on either venue are skipped, never counted as zero funding.",
            "OKX payments are spread evenly over the hours of their interval.",
            "Basis and execution prices at close are not modelled.",
        ],
    }


def summarize(reports: list[MarketReport], days: int) -> list[str]:
    lines: list[str] = []
    by_sym = {r.symbol: r for r in reports}
    for sym in ("BTC-USD", "ETH-USD", "SOL-USD"):
        r = by_sym.get(sym)
        if not r or not r.view_a:
            continue
        base = sym.removesuffix("-USD")
        for side, attr in (
            ("shorts", "short_strike_cheaper_pct"),
            ("longs", "long_strike_cheaper_pct"),
        ):
            parts = [
                f"{_pct(getattr(v, attr))} vs {VENUE_LABEL[v.other_venue]}"
                for v in r.view_a
                if getattr(v, attr) is not None
            ]
            if parts:
                lines.append(
                    f"Over the last {days} days, Strike was the cheaper venue for "
                    f"{', '.join(parts)} of hours for {base} {side} (funding only)."
                )
    pairs = [
        p for r in reports if not r.ticker_only for p in r.pairs if p.cum_net_30d_usd is not None
    ]
    if pairs:
        pos = [p for p in pairs if p.cum_net_30d_usd > 0]  # type: ignore[operator]
        lines.append(
            f"Of {len(pairs)} market and direction pairs, {len(pos)} had a positive estimated "
            f"{days}-day net after taker fees and slippage; {len(pairs) - len(pos)} did not."
        )
        best = max(pairs, key=lambda p: p.cum_net_30d_usd)  # type: ignore[arg-type,return-value]
        worst = min(pairs, key=lambda p: p.cum_net_30d_usd)  # type: ignore[arg-type,return-value]
        for tag, p in (("Best", best), ("Worst", worst)):
            lines.append(
                f"{tag} pair: {p.symbol.removesuffix('-USD')} {_dir_text(p)} "
                f"{_money(p.cum_net_30d_usd)} estimated over {days} days on ${p.notional_usd:,.0f}."  # type: ignore[arg-type]
            )
    return lines


def _dir_text(p: PairStats) -> str:
    o = VENUE_LABEL[p.other_venue]
    return (
        f"(long Strike / short {o})" if p.direction == LONG_STRIKE else f"(long {o} / short Strike)"
    )


def funding_history_hourly(
    store: Store, markets: dict[str, MarketMap], symbol: str, now_ms: int, days: int
) -> dict[str, list[tuple[int, float | None]]]:
    """Per-venue hourly-normalized funding for charts and the history API.

    None marks a missing hour (never zero).
    """
    m = markets[symbol]
    hours = window_hours(now_ms, days)
    since = (hours[0] - 8 * HOUR) if hours else 0
    out: dict[str, list[tuple[int, float | None]]] = {}
    for venue, present in ((STRIKE, True), (HYPERLIQUID, bool(m.hl_coin)), (OKX, bool(m.okx_inst))):
        if not present:
            continue
        s = hourly_series(store.get_funding(venue, symbol, since), hours)
        out[venue] = [(h, s.get(h)) for h in hours]
    return out


def cumulative_for_symbol(
    store: Store,
    settings: Settings,
    m: MarketMap,
    now_ms: int,
    days: int = 30,
    notional: float = 10_000.0,
) -> dict[str, dict[str, list[tuple[int, float]]]]:
    """Chart 2 data: other_venue -> direction -> cumulative net series."""
    hours = window_hours(now_ms, days)
    since = (hours[0] - 8 * HOUR) if hours else 0
    s = hourly_series(store.get_funding(STRIKE, m.strike, since), hours)
    out: dict[str, dict[str, list[tuple[int, float]]]] = {}
    for other, present in ((HYPERLIQUID, bool(m.hl_coin)), (OKX, bool(m.okx_inst))):
        if not present:
            continue
        o = hourly_series(store.get_funding(other, m.strike, since), hours)
        if not s or not o:
            continue
        cost = round_trip_cost_usd(
            notional,
            settings.fee_rate(STRIKE, "taker", m),
            settings.fee_rate(other, "taker", m),
            settings.report_slippage_bps_per_fill,
        )
        out[other] = {d: cumulative_net_series(carry_series(s, o, d, notional), hours, cost)
                      for d in DIRECTIONS}  # fmt: skip
    return out
