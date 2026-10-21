import pytest

from app import report as r
from app.config import HYPERLIQUID, OKX, STRIKE, SYMBOL_MAP, Settings
from app.db import Store
from app.models import FundingPoint

HOUR = r.HOUR
T0 = 1_790_000_000_000 // HOUR * HOUR
HOURS = [T0 + i * HOUR for i in range(10)]


def series(vals):
    return {h: v for h, v in zip(HOURS, vals, strict=True) if v is not None}


# --- hourly normalization ---------------------------------------------------
def test_hourly_series_spreads_8h_rate_evenly():
    hours = [T0 + i * HOUR for i in range(16)]
    # settlement at T0+8h covers hours T0..T0+7h ; settlement at T0+16h covers the next 8
    pts = [FundingPoint(T0 + 8 * HOUR, 0.0008, 8.0), FundingPoint(T0 + 16 * HOUR, -0.0016, 8.0)]
    s = r.hourly_series(pts, hours)
    assert len(s) == 16
    assert s[T0] == pytest.approx(0.0001) and s[T0 + 7 * HOUR] == pytest.approx(0.0001)
    assert s[T0 + 8 * HOUR] == pytest.approx(-0.0002)


def test_hourly_series_1h_with_ms_offset_and_gap():
    hours = [T0 + i * HOUR for i in range(4)]
    pts = [FundingPoint(T0 + HOUR + 41, 0.00001, 1.0), FundingPoint(T0 + 3 * HOUR, 0.00002, 1.0)]
    s = r.hourly_series(pts, hours)
    assert s == {T0: 0.00001, T0 + 2 * HOUR: 0.00002}  # hour T0+1h missing, not zero


def test_window_hours_excludes_current_hour():
    now = T0 + 90 * 60_000
    hrs = r.window_hours(now, 1)
    # now is 30 min into hour T0+1h, so the last completed hour starts at T0
    assert len(hrs) == 24 and hrs[-1] == T0 and hrs[0] == T0 - 23 * HOUR


# --- hand-worked report scenario ---------------------------------------------
# Strike 0.00001/h for 10h. Other: 0.0001 for hours 0-4, 0.000005 for hours 5-9. N=10,000.
#   long Strike / short other carry: +0.90 x5, -0.05 x5  -> sum 4.25, avg 0.425
#   cost = 10000*(2*0.0005 + 2*0.00045) + 4*10000*2/1e4 = 19 + 8 = 27
S = series([0.00001] * 10)
OTH = series([0.0001] * 5 + [0.000005] * 5)
COST = r.round_trip_cost_usd(10_000, 0.0005, 0.00045, 2.0)


def test_round_trip_cost():
    assert pytest.approx(27.0) == COST


def test_pair_stats_long_strike_exact():
    p = r.pair_stats("X-USD", HYPERLIQUID, r.LONG_STRIKE, S, OTH, HOURS, 10_000, COST)
    assert p.hours_compared == 10
    assert p.pct_hours_positive == pytest.approx(0.5)
    assert p.avg_carry_usd_per_hour == pytest.approx(0.425)
    assert p.days_to_cover_costs == pytest.approx(27 / (0.425 * 24))  # 2.647
    assert p.cum_net_30d_usd == pytest.approx(4.25 - 27)
    assert p.cum_net_7d_usd == pytest.approx(4.25 - 27)
    assert p.longest_favorable_hours == 5


def test_pair_stats_reverse_direction_exact():
    p = r.pair_stats("X-USD", HYPERLIQUID, r.LONG_OTHER, S, OTH, HOURS, 10_000, COST)
    assert p.pct_hours_positive == pytest.approx(0.5)
    assert p.avg_carry_usd_per_hour == pytest.approx(-0.425)
    assert p.days_to_cover_costs is None  # negative carry never covers costs
    assert p.cum_net_30d_usd == pytest.approx(-4.25 - 27)
    assert p.longest_favorable_hours == 5  # the +0.05 hours 5-9


def test_missing_hours_are_skipped_and_break_streaks():
    o = series([0.0001, 0.0001, None, 0.0001, 0.0001, 0.0001, None, None, 0.0001, 0.0001])
    carry = r.carry_series(S, o, r.LONG_STRIKE, 10_000)
    assert len(carry) == 7  # three hours with no data are not zero
    assert r.longest_streak(carry, HOURS) == 3
    p = r.pair_stats("X-USD", OKX, r.LONG_STRIKE, S, o, HOURS, 10_000, COST)
    assert p.hours_compared == 7 and p.pct_hours_positive == 1.0


def test_no_overlap_returns_none_not_zero():
    p = r.pair_stats("X-USD", OKX, r.LONG_STRIKE, S, {}, HOURS, 10_000, COST)
    assert p.hours_compared == 0 and p.pct_hours_positive is None and p.cum_net_30d_usd is None


def test_7d_window_uses_last_168_hours_only():
    hours = [T0 + i * HOUR for i in range(240)]
    # carry +1.0 per hour in the first 72h, then 0.5 per hour afterwards
    st = {h: 0.0 for h in hours}
    ot = {h: (0.0001 if i < 72 else 0.00005) for i, h in enumerate(hours)}
    p = r.pair_stats("X-USD", OKX, r.LONG_STRIKE, st, ot, hours, 10_000, 10.0)
    assert p.cum_net_30d_usd == pytest.approx(72 * 1.0 + 168 * 0.5 - 10)
    assert p.cum_net_7d_usd == pytest.approx(168 * 0.5 - 10)


# --- View A -------------------------------------------------------------------
def test_view_a_cheaper_shares():
    st = series([0.0001, 0.0001, 0.0002, 0.0, 0.0, None, 0.0, 0.0, 0.0, 0.0])
    ot = series([0.0002, 0.0001, 0.0001, 0.0001, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    v = r.view_a_stats(OKX, st, ot)
    # common hours: 0,1,2,3,4,6,7,8,9 (9 hours). diffs strike-other:
    # -1e-4(long cheaper), 0, +1e-4 (short cheaper), -1e-4 (long cheaper), 0,0,0,0,0
    assert v.hours_compared == 9
    assert v.long_strike_cheaper_pct == pytest.approx(2 / 9)
    assert v.short_strike_cheaper_pct == pytest.approx(1 / 9)
    assert v.equal_pct == pytest.approx(6 / 9)


# --- cumulative series for the chart ------------------------------------------
def test_cumulative_net_series_values():
    carry = r.carry_series(S, OTH, r.LONG_STRIKE, 10_000)
    ser = r.cumulative_net_series(carry, HOURS, COST)
    assert ser[0] == (HOURS[0], -27.0)
    assert ser[1] == (HOURS[0] + HOUR, pytest.approx(-27 + 0.9))
    assert ser[5][1] == pytest.approx(-27 + 4.5)
    assert ser[-1] == (HOURS[-1] + HOUR, pytest.approx(-27 + 4.25))
    assert len(ser) == 11
    assert r.cumulative_net_series({}, [], 5) == []


def test_cumulative_gap_adds_nothing():
    carry = {HOURS[0]: 1.0, HOURS[2]: 1.0}
    ser = r.cumulative_net_series(carry, HOURS[:3], 0.0)
    assert [v for _, v in ser] == [0.0, 1.0, 1.0, 2.0]


# --- build_report from a store -------------------------------------------------
def seed(store, now):
    hours = r.window_hours(now, 30)
    for sym, strike_rate, hl_rate, okx_rate_8h in (("BTC-USD", 0.00001, 0.0001, 0.0008),):
        store.upsert_funding(STRIKE, sym, [FundingPoint(h + HOUR, strike_rate, 1.0) for h in hours])
        store.upsert_funding(
            HYPERLIQUID, sym, [FundingPoint(h + HOUR + 41, hl_rate, 1.0) for h in hours]
        )
        store.upsert_funding(
            OKX, sym, [FundingPoint(h + HOUR, okx_rate_8h, 8.0) for h in hours[7::8]]
        )


def test_build_report_end_to_end():
    store, now = Store(":memory:"), T0 + 30 * 60_000
    seed(store, now)
    rep = r.build_report(store, Settings(), [SYMBOL_MAP["BTC-USD"], SYMBOL_MAP["XAU-USD"]], now)
    btc = rep["markets"][0]
    assert btc["hours_with_data"]["strike"] == 720 and btc["hours_with_data"]["hyperliquid"] == 720
    pairs = {(p["other_venue"], p["direction"]): p for p in btc["pairs"]}
    hl_long = pairs[(HYPERLIQUID, r.LONG_STRIKE)]
    assert hl_long["pct_hours_positive"] == 1.0
    assert hl_long["avg_carry_usd_per_hour"] == pytest.approx(0.9)
    assert hl_long["cum_net_30d_usd"] == pytest.approx(0.9 * 720 - 27)
    assert hl_long["cum_net_7d_usd"] == pytest.approx(0.9 * 168 - 27)
    assert hl_long["longest_favorable_hours"] == 720
    assert pairs[(HYPERLIQUID, r.LONG_OTHER)]["days_to_cover_costs"] is None
    # OKX 8h 0.0008 -> 0.0001/h, same carry vs Strike 0.00001 ; cost uses OKX taker 0.0005
    okx_long = pairs[(OKX, r.LONG_STRIKE)]
    assert okx_long["avg_carry_usd_per_hour"] == pytest.approx(0.9)
    assert okx_long["round_trip_cost_usd"] == pytest.approx(10 + 10 + 8)
    # XAU has no history: present with no pairs, nothing invented
    assert rep["markets"][1]["pairs"] == []
    assert any("BTC shorts" in s for s in rep["summary"])
    assert "Past funding does not predict future funding." in rep["assumptions"]["notes"]


def test_summary_text_names_cheaper_share():
    store, now = Store(":memory:"), T0 + 30 * 60_000
    seed(store, now)
    rep = r.build_report(store, Settings(), [SYMBOL_MAP["BTC-USD"]], now)
    # Strike (0.00001) lower than others (0.0001): longs cheaper 100%, shorts 0%
    long_line = next(s for s in rep["summary"] if "BTC longs" in s)
    short_line = next(s for s in rep["summary"] if "BTC shorts" in s)
    assert "100% of hours for BTC longs vs Hyperliquid, 100% vs OKX" in long_line
    assert "0% of hours for BTC shorts vs Hyperliquid" in short_line


def test_funding_history_hourly_marks_missing_as_none():
    store, now = Store(":memory:"), T0 + 30 * 60_000
    hours = r.window_hours(now, 1)
    store.upsert_funding(
        STRIKE, "BTC-USD", [FundingPoint(h + HOUR, 0.00001, 1.0) for h in hours[:20]]
    )
    out = r.funding_history_hourly(store, SYMBOL_MAP, "BTC-USD", now, 1)
    assert [v for _, v in out[STRIKE]][:20] == [0.00001] * 20
    assert [v for _, v in out[STRIKE]][20:] == [None] * 4
    assert out[HYPERLIQUID][0][1] is None  # no data stays None


def test_cumulative_for_symbol_has_both_directions_per_competitor():
    store, now = Store(":memory:"), T0 + 30 * 60_000
    seed(store, now)
    out = r.cumulative_for_symbol(store, Settings(), SYMBOL_MAP["BTC-USD"], now)
    assert set(out) == {HYPERLIQUID, OKX}
    assert set(out[OKX]) == {r.LONG_STRIKE, r.LONG_OTHER}
    ser = out[HYPERLIQUID][r.LONG_STRIKE]
    assert ser[0][1] == pytest.approx(-27.0) and ser[-1][1] == pytest.approx(0.9 * 720 - 27)
