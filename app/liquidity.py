"""Liquidity quality: how much of Strike's SLP depth is borrowed (0% credit) capital.

Metrics, from the stored LiquiditySample series:
    borrowed_share = Borrow & Earn debt / SLP TVL
    vault_apr      = Strike's published SLP apr (no upstream history; sampled from first run)
    derived_apr    = share price growth over the APR window (30 days), annualized (simple)
    volume_to_tvl  = Strike 24h perps volume / SLP TVL

A value is None (shown as n/a) unless every input is present. Ratios are only formed from
inputs of the same sample, so a missing half never produces a partial ratio. Trends need a
baseline sample near `trend_days` ago; debt history only exists from first run, so the
borrowed share trend is None until then.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any

from app.config import STALE_AFTER_MS, STRIKE, LiquidityConfig
from app.models import LiquiditySample
from app.state import LiveState

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
YEAR_DAYS = 365.0
BACKFILL_DAYS = 70  # 30 day series plus its 30 day APR baselines (daily rows past 30 days)
KEEP_DAYS = 75
APR_TOLERANCE_MS = 12 * HOUR_MS  # baselines beyond 30 days are daily rows
THIN_AFTER_DAYS = 7  # rows older than this are read one per hour

Metric = Callable[[LiquiditySample], float | None]


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    """numerator / denominator, or None if either is missing or the denominator is not > 0."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def borrowed_share(s: LiquiditySample) -> float | None:
    return ratio(s.debt_total_usd, s.slp_tvl_usd)


def volume_to_tvl(s: LiquiditySample) -> float | None:
    return ratio(s.volume_24h_usd, s.slp_tvl_usd)


def strike_volume_24h(state: LiveState, now_ms: int) -> float | None:
    """Sum of Strike's per-market 24h volume, only from fresh quotes that all report volume."""
    quotes = state.quotes.get(STRIKE) or {}
    ok_ms = state.last_ok_ms.get(STRIKE)
    if not quotes or ok_ms is None or now_ms - ok_ms > STALE_AFTER_MS:
        return None
    vols = [q.volume_24h_usd for q in quotes.values()]
    if any(v is None for v in vols):
        return None  # a total over some markets would understate volume
    return float(sum(vols))


def latest(
    samples: Sequence[LiquiditySample], metric: Metric, now_ms: int, max_age_ms: int
) -> tuple[LiquiditySample, float] | None:
    """Newest sample within `max_age_ms` where `metric` is computable."""
    for s in reversed(samples):
        if now_ms - s.ts_ms > max_age_ms:
            return None
        if s.ts_ms <= now_ms and (v := metric(s)) is not None:
            return s, v
    return None


def nearest(
    samples: Sequence[LiquiditySample],
    metric: Metric,
    target_ms: int,
    tolerance_ms: int,
    times: Sequence[int] | None = None,
) -> tuple[LiquiditySample, float] | None:
    """Closest sample to `target_ms`, within the tolerance, where `metric` is computable."""
    ts = times if times is not None else [s.ts_ms for s in samples]
    lo, hi = bisect_left(ts, target_ms) - 1, bisect_left(ts, target_ms)
    while lo >= 0 or hi < len(samples):
        d_lo = target_ms - ts[lo] if lo >= 0 else None
        d_hi = ts[hi] - target_ms if hi < len(samples) else None
        take_lo = d_hi is None or (d_lo is not None and d_lo <= d_hi)
        d = d_lo if take_lo else d_hi
        if d is None or d > tolerance_ms:
            return None
        s = samples[lo if take_lo else hi]
        lo, hi = (lo - 1, hi) if take_lo else (lo, hi + 1)
        if (v := metric(s)) is not None:
            return s, v
    return None


def derived_apr(
    samples: Sequence[LiquiditySample],
    at_ms: int,
    window_days: float,
    tolerance_ms: int,
    times: Sequence[int] | None = None,
) -> float | None:
    """Share price growth from `window_days` before `at_ms` to `at_ms`, annualized.

    Simple annualization: (price_now / price_then - 1) * 365 / elapsed days, as a fraction
    (0.25 = 25%). The elapsed time is that of the two samples actually used.
    """
    metric: Metric = lambda s: s.slp_share_price  # noqa: E731
    end = nearest(samples, metric, at_ms, tolerance_ms, times)
    start = nearest(samples, metric, at_ms - int(window_days * DAY_MS), tolerance_ms, times)
    if end is None or start is None:
        return None
    elapsed_days = (end[0].ts_ms - start[0].ts_ms) / DAY_MS
    if elapsed_days <= 0 or start[1] <= 0:
        return None
    return (end[1] / start[1] - 1.0) * YEAR_DAYS / elapsed_days


def trend(
    samples: Sequence[LiquiditySample],
    metric: Metric,
    current: tuple[LiquiditySample, float] | None,
    cfg: LiquidityConfig,
    *,
    relative: bool,
    band: float,
) -> dict[str, Any]:
    """Change over `trend_days` and its direction (up, down, flat); None fields if no baseline.

    `relative` compares as a percentage change (volume_to_tvl); otherwise as an absolute
    change in the metric (borrowed share, in fraction points).
    """
    out: dict[str, Any] = {
        "direction": None,
        "change": None,
        "baseline": None,
        "baseline_ts_ms": None,
    }
    if current is None:
        return out
    cur_s, cur_v = current
    target = cur_s.ts_ms - int(cfg.trend_days * DAY_MS)
    base = nearest(samples, metric, target, int(cfg.trend_tolerance_hours * HOUR_MS))
    if base is None:
        return out
    base_s, base_v = base
    if relative:
        if base_v <= 0:
            return out
        change = cur_v / base_v - 1.0
    else:
        change = cur_v - base_v
    direction = "up" if change > band else "down" if change < -band else "flat"
    return {
        "direction": direction,
        "change": change,
        "baseline": base_v,
        "baseline_ts_ms": base_s.ts_ms,
    }


def evaluate_flag(
    share: float | None,
    share_trend: dict[str, Any],
    vtt_trend: dict[str, Any],
    cfg: LiquidityConfig,
) -> dict[str, Any]:
    """Fragile when borrowed share is high AND rising AND volume_to_tvl is flat or falling.

    Three-valued: any condition known to be false gives "ok"; otherwise any unknown input
    gives "insufficient_data"; only all three known true gives "fragile".
    """
    conds: list[tuple[str, bool | None]] = [
        ("borrowed_share_high", None if share is None else share >= cfg.borrowed_share_high),
        (
            "borrowed_share_rising",
            None if share_trend["direction"] is None else share_trend["direction"] == "up",
        ),
        (
            "volume_to_tvl_flat_or_falling",
            None if vtt_trend["direction"] is None else vtt_trend["direction"] != "up",
        ),
    ]
    if any(v is False for _, v in conds):
        status = "ok"
    elif any(v is None for _, v in conds):
        status = "insufficient_data"
    else:
        status = "fragile"
    return {
        "status": status,
        "conditions": {name: v for name, v in conds},
        "thresholds": asdict(cfg),
    }


def _point(s: LiquiditySample, derived: float | None) -> dict[str, Any]:
    return {
        "ts_ms": s.ts_ms,
        "borrowed_share": borrowed_share(s),
        "vault_apr": s.published_apr,
        "derived_apr": derived,
        "volume_to_tvl": volume_to_tvl(s),
        "slp_tvl_usd": s.slp_tvl_usd,
        "debt_total_usd": s.debt_total_usd,
        "volume_24h_usd": s.volume_24h_usd,
    }


def series(
    samples: Sequence[LiquiditySample],
    now_ms: int,
    days: int,
    cfg: LiquidityConfig,
    times: Sequence[int],
) -> list[dict[str, Any]]:
    """Points over the last `days`: hourly for 7 days or less, else daily.

    Hourly buckets keep their last sample. Daily buckets keep their first sample (UTC
    midnight), where the backfilled daily volume sits. Each point's APR is trailing.
    """
    bucket = HOUR_MS if days <= 7 else DAY_MS
    pick_last = bucket == HOUR_MS
    start = now_ms - days * DAY_MS
    chosen: dict[int, LiquiditySample] = {}
    for s in samples:
        if s.ts_ms < start or s.ts_ms > now_ms:
            continue
        key = s.ts_ms // bucket
        if pick_last or key not in chosen:
            chosen[key] = s
    return [
        _point(s, derived_apr(samples, s.ts_ms, cfg.apr_window_days, APR_TOLERANCE_MS, times))
        for _, s in sorted(chosen.items())
    ]


def build(samples: Sequence[LiquiditySample], cfg: LiquidityConfig, now_ms: int) -> dict[str, Any]:
    """Current metrics, 7 day trends, the flag and the 7d/30d series. Samples ascending."""
    samples = [s for s in samples if s.ts_ms <= now_ms]
    times = [s.ts_ms for s in samples]

    cur_share = latest(samples, borrowed_share, now_ms, STALE_AFTER_MS)
    cur_vtt = latest(samples, volume_to_tvl, now_ms, STALE_AFTER_MS)
    cur_tvl = latest(samples, lambda s: s.slp_tvl_usd, now_ms, STALE_AFTER_MS)
    cur_debt = latest(samples, lambda s: s.debt_total_usd, now_ms, STALE_AFTER_MS)
    cur_vol = latest(samples, lambda s: s.volume_24h_usd, now_ms, STALE_AFTER_MS)
    cur_sp = latest(samples, lambda s: s.slp_share_price, now_ms, STALE_AFTER_MS)
    cur_apr = latest(samples, lambda s: s.published_apr, now_ms, STALE_AFTER_MS)
    derived = None
    if cur_sp is not None:
        derived = derived_apr(
            samples, cur_sp[0].ts_ms, cfg.apr_window_days, APR_TOLERANCE_MS, times
        )

    share = None if cur_share is None else cur_share[1]
    share_trend = trend(
        samples, borrowed_share, cur_share, cfg, relative=False, band=cfg.borrowed_share_flat_band
    )
    vtt_trend = trend(
        samples, volume_to_tvl, cur_vtt, cfg, relative=True, band=cfg.volume_to_tvl_rising
    )
    with_debt = [s.ts_ms for s in samples if s.debt_total_usd is not None]
    debt_by_asset = None
    if cur_debt is not None:
        debt_by_asset = cur_debt[0].debt_by_asset
    return {
        "current": {
            "sample_ts_ms": None if cur_share is None else cur_share[0].ts_ms,
            "borrowed_share": share,
            "vault_apr": None if cur_apr is None else cur_apr[1],
            "derived_apr": derived,
            "volume_to_tvl": None if cur_vtt is None else cur_vtt[1],
            "slp_tvl_usd": None if cur_tvl is None else cur_tvl[1],
            "debt_total_usd": None if cur_debt is None else cur_debt[1],
            "debt_by_asset": debt_by_asset,
            "volume_24h_usd": None if cur_vol is None else cur_vol[1],
        },
        "trend_7d": {"borrowed_share": share_trend, "volume_to_tvl": vtt_trend},
        "flag": evaluate_flag(share, share_trend, vtt_trend, cfg),
        "debt_history_started_ms": with_debt[0] if with_debt else None,
        "series_7d": series(samples, now_ms, 7, cfg, times),
        "series_30d": series(samples, now_ms, 30, cfg, times),
    }


def summary(out: dict[str, Any], cfg: LiquidityConfig) -> str:
    """One plain sentence for the flag. n/a is stated, never filled in."""
    share = out["current"]["borrowed_share"]
    flag = out["flag"]["status"]
    if share is None:
        return "n/a: borrowed share of SLP needs current Borrow & Earn debt and SLP TVL."
    pct = f"{share * 100:.1f}%"
    st = out["trend_7d"]["borrowed_share"]
    vt = out["trend_7d"]["volume_to_tvl"]
    if flag == "fragile":
        return (
            f"Borrowed share of SLP is {pct} (at or above {cfg.borrowed_share_high * 100:.0f}%) "
            f"and rising over {cfg.trend_days:g} days while volume per TVL is flat or falling. "
            "Depth funded by borrowed capital may not hold."
        )
    if flag == "ok":
        if share < cfg.borrowed_share_high:
            why = f"below the {cfg.borrowed_share_high * 100:.0f}% threshold"
        elif st["direction"] != "up":
            why = f"not rising over {cfg.trend_days:g} days"
        else:
            why = "with volume per TVL rising"
        return f"Borrowed share of SLP is {pct}, {why}."
    missing = []
    if st["direction"] is None:
        missing.append(f"{cfg.trend_days:g} days of debt history")
    if vt["direction"] is None:
        missing.append(f"a {cfg.trend_days:g} day volume per TVL baseline")
    return f"Borrowed share of SLP is {pct}. Flag n/a: needs {' and '.join(missing)}."
