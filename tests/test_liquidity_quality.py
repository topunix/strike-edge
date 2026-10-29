import pytest

from app import liquidity as lq
from app.config import STRIKE, LiquidityConfig, Settings
from app.models import LiquiditySample, Quote
from app.state import LiveState

NOW = 1_792_600_000_000
HOUR, DAY = lq.HOUR_MS, lq.DAY_MS
CFG = LiquidityConfig()


def sample(ts, tvl=1000.0, debt=None, vol=None, sp=1.0):
    return LiquiditySample(ts, tvl, sp, debt, None if debt is None else {"ADA": debt}, vol)


def hourly(days, *, debt_share, vol_ratio, sp_step=0.0, start=None):
    """Hourly samples ending at NOW. debt_share/vol_ratio are callables of hours-ago."""
    out = []
    n = int(days * 24)
    for k in range(n, -1, -1):
        ts = NOW - k * HOUR
        sp = 1.0 + sp_step * (n - k)
        out.append(sample(ts, 1000.0, debt_share(k) * 1000.0, vol_ratio(k) * 1000.0, sp))
    return out


# --- ratio math -----------------------------------------------------------------
def test_ratio_math():
    s = sample(NOW, tvl=930_000.0, debt=406_000.0, vol=10_000_000.0)
    assert lq.borrowed_share(s) == pytest.approx(406_000 / 930_000)
    assert lq.volume_to_tvl(s) == pytest.approx(10_000_000 / 930_000)
    assert lq.ratio(0.0, 100.0) == 0.0  # zero debt is a real 0%, not missing


@pytest.mark.parametrize("tvl", [0.0, -5.0, None])
def test_ratio_is_none_without_a_positive_denominator(tvl):
    s = sample(NOW, tvl=tvl, debt=100.0, vol=100.0)
    assert lq.borrowed_share(s) is None and lq.volume_to_tvl(s) is None


def test_ratio_is_none_when_numerator_missing():
    s = sample(NOW, tvl=1000.0, debt=None, vol=None)
    assert lq.borrowed_share(s) is None and lq.volume_to_tvl(s) is None


def test_vault_apr_from_share_price_series():
    # +0.25% over exactly 7 days -> 0.0025 * 365 / 7
    samples = [sample(NOW - 7 * DAY, sp=1.0), sample(NOW, sp=1.0025)]
    apr = lq.vault_apr(samples, NOW, 7, 6 * HOUR, [s.ts_ms for s in samples])
    assert apr == pytest.approx(0.0025 * 365 / 7)


def test_vault_apr_uses_actual_elapsed_time_of_the_samples():
    samples = [sample(NOW - 7 * DAY + 2 * HOUR, sp=1.0), sample(NOW, sp=1.0025)]
    apr = lq.vault_apr(samples, NOW, 7, 6 * HOUR, [s.ts_ms for s in samples])
    assert apr == pytest.approx(0.0025 * 365 / (7 - 2 / 24))


def test_vault_apr_none_without_baseline_or_price():
    young = [sample(NOW - DAY, sp=1.0), sample(NOW, sp=1.01)]
    assert lq.vault_apr(young, NOW, 7, 6 * HOUR, [s.ts_ms for s in young]) is None
    no_price = [sample(NOW - 7 * DAY, sp=None), sample(NOW, sp=None)]
    assert lq.vault_apr(no_price, NOW, 7, 6 * HOUR, [s.ts_ms for s in no_price]) is None


# --- trend ----------------------------------------------------------------------
def share_trend(cur, base, cfg=CFG, base_age_days=7.0):
    samples = [
        sample(int(NOW - base_age_days * DAY), tvl=1000.0, debt=base * 1000.0),
        sample(NOW, tvl=1000.0, debt=cur * 1000.0),
    ]
    now = lq.latest(samples, lq.borrowed_share, NOW, 5 * 60_000)
    return lq.trend(samples, lq.borrowed_share, now, cfg, relative=False, band=0.02)


@pytest.mark.parametrize(
    "cur,base,direction",
    [(0.46, 0.40, "up"), (0.40, 0.46, "down"), (0.41, 0.40, "flat"), (0.42, 0.40, "flat")],
)
def test_borrowed_share_trend_direction_uses_the_flat_band(cur, base, direction):
    t = share_trend(cur, base)
    assert t["direction"] == direction
    assert t["change"] == pytest.approx(cur - base)


def test_trend_is_none_until_a_7d_old_sample_exists():
    t = share_trend(0.46, 0.40, base_age_days=3.0)
    assert t == {"direction": None, "change": None, "baseline": None, "baseline_ts_ms": None}


def test_trend_tolerance_is_configurable():
    cfg = LiquidityConfig(trend_tolerance_hours=1.0)
    assert share_trend(0.46, 0.40, cfg, base_age_days=6.9)["direction"] is None
    assert share_trend(0.46, 0.40, LiquidityConfig(), base_age_days=6.9)["direction"] == "up"


def test_volume_to_tvl_trend_is_relative():
    def t(cur, base):
        samples = [sample(NOW - 7 * DAY, vol=base * 1000), sample(NOW, vol=cur * 1000)]
        c = lq.latest(samples, lq.volume_to_tvl, NOW, 5 * 60_000)
        return lq.trend(samples, lq.volume_to_tvl, c, CFG, relative=True, band=0.25)

    assert t(13.0, 10.0)["direction"] == "up"  # +30%
    assert t(12.0, 10.0)["direction"] == "flat"  # +20%
    assert t(7.0, 10.0)["direction"] == "down"  # -30%
    assert t(5.0, 0.0)["direction"] is None  # no relative change from a zero baseline


# --- flag -----------------------------------------------------------------------
def tr(direction):
    return {"direction": direction, "change": 0.0, "baseline": 0.0, "baseline_ts_ms": 0}


@pytest.mark.parametrize(
    "share,bs_dir,vtt_dir,status",
    [
        (0.45, "up", "flat", "fragile"),
        (0.45, "up", "down", "fragile"),
        (0.40, "up", "flat", "fragile"),  # at the threshold counts as high
        (0.45, "up", "up", "ok"),  # volume is keeping pace
        (0.45, "flat", "flat", "ok"),  # high but not rising
        (0.45, "down", "down", "ok"),
        (0.30, "up", "flat", "ok"),  # rising but not high
    ],
)
def test_flag_requires_high_and_rising_and_volume_not_rising(share, bs_dir, vtt_dir, status):
    assert lq.evaluate_flag(share, tr(bs_dir), tr(vtt_dir), CFG)["status"] == status


@pytest.mark.parametrize(
    "share,bs_dir,vtt_dir,status",
    [
        (0.45, None, "flat", "insufficient_data"),  # no 7d debt history yet
        (0.45, "up", None, "insufficient_data"),
        (None, None, None, "insufficient_data"),
        (0.30, None, None, "ok"),  # known to be below the threshold, trends cannot change that
        (0.45, "flat", None, "ok"),  # known not rising
        (None, "down", "flat", "ok"),
    ],
)
def test_flag_with_missing_inputs_is_never_fragile(share, bs_dir, vtt_dir, status):
    assert lq.evaluate_flag(share, tr(bs_dir), tr(vtt_dir), CFG)["status"] == status


def test_flag_thresholds_come_from_config():
    cfg = LiquidityConfig(borrowed_share_high=0.60)
    assert lq.evaluate_flag(0.45, tr("up"), tr("flat"), cfg)["status"] == "ok"
    assert (
        lq.evaluate_flag(0.45, tr("up"), tr("flat"), cfg)["thresholds"]["borrowed_share_high"]
        == 0.6
    )


def test_liquidity_config_defaults_and_env_override():
    assert Settings().liquidity == LiquidityConfig()
    s = Settings.from_env({"LQ_BORROWED_SHARE_HIGH": "0.5", "LQ_TREND_TOLERANCE_HOURS": "3"})
    assert s.liquidity.borrowed_share_high == 0.5 and s.liquidity.trend_tolerance_hours == 3.0
    assert s.liquidity.volume_to_tvl_rising == 0.25


# --- build: current, missing data, series ------------------------------------------
def test_build_fragile_end_to_end():
    samples = hourly(
        8,
        debt_share=lambda k: 0.46 - 0.0005 * k,  # 0.46 now, lower a week ago
        vol_ratio=lambda k: 10.0,  # flat
        sp_step=0.0001,
    )
    out = lq.build(samples, LiquidityConfig(apr_window_days=7), NOW)
    cur = out["current"]
    assert cur["borrowed_share"] == pytest.approx(0.46)
    assert cur["volume_to_tvl"] == pytest.approx(10.0)
    assert cur["vault_apr"] == pytest.approx((1.0192 / 1.0024 - 1) * 365 / 7)
    assert out["trend_7d"]["borrowed_share"]["direction"] == "up"
    assert out["trend_7d"]["volume_to_tvl"]["direction"] == "flat"
    assert out["flag"]["status"] == "fragile"
    assert len(out["series_7d"]) == 169 and len(out["series_30d"]) <= 9


def test_build_apr_window_defaults_to_30_days_and_is_na_without_that_history():
    assert CFG.apr_window_days == 30.0
    short = hourly(8, debt_share=lambda k: 0.4, vol_ratio=lambda k: 10.0, sp_step=0.0001)
    assert lq.build(short, CFG, NOW)["current"]["vault_apr"] is None  # only 8 days of prices
    long = hourly(31, debt_share=lambda k: 0.4, vol_ratio=lambda k: 10.0, sp_step=0.0001)
    n = 31 * 24
    expected = ((1 + 0.0001 * n) / (1 + 0.0001 * (n - 720)) - 1) * 365 / 30
    assert lq.build(long, CFG, NOW)["current"]["vault_apr"] == pytest.approx(expected)


def test_build_has_no_debt_trend_before_a_week_of_snapshots():
    samples = hourly(2, debt_share=lambda k: 0.45, vol_ratio=lambda k: 10.0)
    out = lq.build(samples, CFG, NOW)
    assert out["current"]["borrowed_share"] == pytest.approx(0.45)
    assert out["trend_7d"]["borrowed_share"]["direction"] is None
    assert out["flag"]["status"] == "insufficient_data"
    assert out["debt_history_started_ms"] == NOW - 48 * HOUR


def test_build_missing_debt_is_na_not_a_partial_ratio():
    samples = [
        sample(NOW - 60_000 * k, tvl=1000.0, debt=None, vol=5000.0) for k in range(10, -1, -1)
    ]
    out = lq.build(samples, CFG, NOW)
    cur = out["current"]
    assert cur["borrowed_share"] is None and cur["debt_total_usd"] is None
    assert cur["slp_tvl_usd"] == 1000.0 and cur["volume_to_tvl"] == 5.0
    assert out["flag"]["status"] == "insufficient_data"


def test_build_missing_tvl_makes_every_ratio_na():
    samples = [sample(NOW, tvl=None, debt=400.0, vol=5000.0)]
    cur = lq.build(samples, CFG, NOW)["current"]
    assert cur["borrowed_share"] is None and cur["volume_to_tvl"] is None
    assert cur["debt_total_usd"] == 400.0  # the raw input is still reported


def test_build_missing_volume_is_na_for_volume_to_tvl_only():
    samples = [sample(NOW, tvl=1000.0, debt=400.0, vol=None)]
    cur = lq.build(samples, CFG, NOW)["current"]
    assert cur["borrowed_share"] == pytest.approx(0.4) and cur["volume_to_tvl"] is None


def test_build_ignores_ratio_halves_from_different_samples():
    # newest sample has only TVL, an older one only debt: never mixed into one ratio
    samples = [sample(NOW - 60_000, tvl=None, debt=400.0), sample(NOW, tvl=1000.0, debt=None)]
    assert lq.build(samples, CFG, NOW)["current"]["borrowed_share"] is None


def test_build_stale_or_empty_samples_are_na():
    old = [sample(NOW - 10 * 60_000, tvl=1000.0, debt=400.0, vol=5000.0)]
    cur = lq.build(old, CFG, NOW)["current"]
    assert all(cur[k] is None for k in ("borrowed_share", "volume_to_tvl", "slp_tvl_usd"))
    out = lq.build([], CFG, NOW)
    assert out["current"]["borrowed_share"] is None and out["series_7d"] == []
    assert out["flag"]["status"] == "insufficient_data"


def test_series_daily_points_use_midnight_samples_with_backfilled_volume():
    day0 = (NOW // DAY - 3) * DAY
    samples = [sample(day0 + h * HOUR, vol=(7000.0 if h == 0 else None)) for h in range(0, 72)]
    pts = lq.series(samples, NOW, 30, CFG, [s.ts_ms for s in samples])
    assert [p["ts_ms"] for p in pts] == [day0, day0 + DAY, day0 + 2 * DAY]
    assert pts[0]["volume_to_tvl"] == pytest.approx(7.0)


# --- live volume input --------------------------------------------------------------
def quote(sym, vol):
    return Quote(STRIKE, sym, sym, 1.0, 1.0, None, vol, NOW, NOW)


def test_strike_volume_sums_fresh_quotes():
    st = LiveState()
    st.set_quotes(STRIKE, {"A-USD": quote("A-USD", 1_000.0), "B-USD": quote("B-USD", 2_500.0)}, NOW)
    assert lq.strike_volume_24h(st, NOW + 1000) == 3_500.0


def test_strike_volume_is_none_if_any_market_lacks_volume_or_data_is_stale():
    st = LiveState()
    st.set_quotes(STRIKE, {"A-USD": quote("A-USD", 1_000.0), "B-USD": quote("B-USD", None)}, NOW)
    assert lq.strike_volume_24h(st, NOW) is None
    st.set_quotes(STRIKE, {"B-USD": quote("B-USD", 2_000.0)}, NOW)
    assert lq.strike_volume_24h(st, NOW) == 3_000.0
    assert lq.strike_volume_24h(st, NOW + 6 * 60_000) is None  # quotes older than 5 minutes
    assert lq.strike_volume_24h(LiveState(), NOW) is None
