"""Sign regression (real cached hour), per-market slippage and the outlier guard."""

from datetime import UTC, datetime

import httpx
import pytest

from app import report as r
from app.adapters.base import TokenBucket, UpstreamClient
from app.config import HYPERLIQUID, OKX, STRIKE, SYMBOL_MAP, Settings
from app.db import Store
from app.models import Book, FundingPoint

HOUR = r.HOUR


def ms(*a):
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


# --- regression: real hour from the cached history ---------------------------------
# Hour starting 2026-10-17T11:00Z. Raw values as stored (Strike/HL hourly, stamped at the
# hour end; OKX 8h payment stamped 16:00Z covering 08:00 to 16:00).
#
# SOL raw:  Strike +6.6432e-06 /1h   HL +7.2053e-06 /1h   OKX -2.11878e-05 /8h
#   hourly: +6.6432e-06             +7.2053e-06            -2.64848e-06
#   $10k LONG funding (long pays when rate > 0):
#     Strike -0.0664 USD (pays)   HL -0.0721 USD (pays)   OKX +0.0265 USD (receives)
#   Strike is cheaper for a long than HL (pays less) but not than OKX (OKX pays the long).
# ETH raw:  Strike -3.32369e-05 /1h  HL -8.124e-07 /1h   OKX +7.98397e-05 /8h
#   hourly: -3.32369e-05            -8.124e-07             +9.97996e-06
#   $10k LONG funding: Strike +0.3324 (receives)  HL +0.0081 (receives)  OKX -0.0998 (pays)
#   Strike is the cheapest venue for an ETH long this hour.
SOL_RAW = {
    STRIKE: (6.643153e-06, 1.0),
    HYPERLIQUID: (7.2053e-06, 1.0),
    OKX: (-2.11878184367e-05, 8.0),
}
ETH_RAW = {
    STRIKE: (-3.3236933e-05, 1.0),
    HYPERLIQUID: (-8.124e-07, 1.0),
    OKX: (7.98396643834e-05, 8.0),
}
STAMP = {
    STRIKE: ms(2026, 10, 17, 12),
    HYPERLIQUID: ms(2026, 10, 17, 12) + 72,
    OKX: ms(2026, 10, 17, 16),
}
HOUR_START = ms(2026, 10, 17, 11)


def hourly_for(raw):
    out = {}
    for venue, (rate, interval) in raw.items():
        pts = [FundingPoint(STAMP[venue], rate, interval)]
        out[venue] = r.hourly_series(pts, [HOUR_START])[HOUR_START]
    return out


def test_sol_hour_signs_and_long_cost():
    h = hourly_for(SOL_RAW)
    assert h[OKX] == pytest.approx(-2.64848e-06, rel=1e-5)  # 8h rate spread over 8 hours
    long_funding = {v: -x * 10_000 for v, x in h.items()}  # +ve rate: long pays
    assert long_funding[STRIKE] == pytest.approx(-0.0664, abs=5e-5)
    assert long_funding[HYPERLIQUID] == pytest.approx(-0.0721, abs=5e-5)
    assert long_funding[OKX] == pytest.approx(+0.0265, abs=5e-5)
    v = r.view_a_stats(HYPERLIQUID, {HOUR_START: h[STRIKE]}, {HOUR_START: h[HYPERLIQUID]})
    assert v.long_strike_cheaper_pct == 1.0 and v.short_strike_cheaper_pct == 0.0
    v = r.view_a_stats(OKX, {HOUR_START: h[STRIKE]}, {HOUR_START: h[OKX]})
    assert v.long_strike_cheaper_pct == 0.0 and v.short_strike_cheaper_pct == 1.0


def test_eth_hour_signs_and_long_cost():
    h = hourly_for(ETH_RAW)
    assert h[OKX] == pytest.approx(9.97996e-06, rel=1e-5)
    long_funding = {v: -x * 10_000 for v, x in h.items()}
    assert long_funding[STRIKE] == pytest.approx(+0.3324, abs=5e-5)
    assert long_funding[HYPERLIQUID] == pytest.approx(+0.0081, abs=5e-5)
    assert long_funding[OKX] == pytest.approx(-0.0998, abs=5e-5)
    assert long_funding[STRIKE] > long_funding[HYPERLIQUID] > long_funding[OKX]
    for other in (HYPERLIQUID, OKX):
        v = r.view_a_stats(other, {HOUR_START: h[STRIKE]}, {HOUR_START: h[other]})
        assert v.long_strike_cheaper_pct == 1.0  # Strike cheapest long this hour


# --- slippage from the current book --------------------------------------------------
def book(bid, ask, qty=1000.0):
    return Book(((bid, qty),), ((ask, qty),), 0, 0)


def test_slippage_measured_above_floor():
    e = r.slippage_from_book(book(99.9, 100.1), 10_000)  # 10 bps each way
    assert e.bps == pytest.approx(10.0) and e.source == "current book walk" and not e.thin


def test_slippage_floor_applied_and_labelled():
    e = r.slippage_from_book(book(99.995, 100.005), 10_000)  # 0.5 bps
    assert e.bps == 2.0 and e.measured_bps == pytest.approx(0.5) and "floor applied" in e.source


def test_slippage_fallback_when_book_missing():
    e = r.slippage_from_book(None, 10_000)
    assert e.bps == 2.0 and "unavailable" in e.source and not e.thin


def test_slippage_thin_book_flags_and_falls_back():
    e = r.slippage_from_book(book(99.0, 101.0, qty=5.0), 10_000)  # only $500 deep
    assert e.thin and e.bps == 2.0 and "too thin" in e.source
    wide = r.slippage_from_book(book(99.0, 101.0), 10_000)  # 100 bps: measured, thin
    assert wide.bps == pytest.approx(100.0) and wide.thin


def test_round_trip_cost_uses_per_venue_slippage():
    # fees 10 + 9 = 19. slippage: 2 fills * 10k * 4bps + 2 fills * 10k * 10bps = 8 + 20 = 28
    assert r.round_trip_cost_usd(10_000, 0.0005, 0.00045, 4.0, 10.0) == pytest.approx(47.0)
    assert r.round_trip_cost_usd(10_000, 0.0005, 0.00045, 2.0) == pytest.approx(27.0)


# --- build_report: per-market slippage recorded, guard and summary exclusion ----------
NOW = ms(2026, 10, 21, 12, 30)


def seed(store, sym, strike_rate, other_rate):
    hours = r.window_hours(NOW, 30)
    store.upsert_funding(STRIKE, sym, [FundingPoint(h + HOUR, strike_rate, 1.0) for h in hours])
    store.upsert_funding(HYPERLIQUID, sym, [FundingPoint(h + HOUR, other_rate, 1.0) for h in hours])


def test_slippage_recorded_per_market_and_used_in_cost():
    store = Store(":memory:")
    seed(store, "ETH-USD", 0.0, 0.00001)
    slip = {
        (STRIKE, "ETH-USD"): r.SlippageEstimate(4.0, "current book walk", 4.0),
        (HYPERLIQUID, "ETH-USD"): r.SlippageEstimate(10.0, "current book walk", 10.0),
    }
    rep = r.build_report(store, Settings(), [SYMBOL_MAP["ETH-USD"]], NOW, slippage=slip)
    m = rep["markets"][0]
    assert m["slippage_used"][STRIKE]["bps"] == 4.0
    assert m["slippage_used"][HYPERLIQUID]["source"] == "current book walk"
    p = m["pairs"][0]
    assert p["round_trip_cost_usd"] == pytest.approx(19 + 28)
    assert p["slippage_bps_strike"] == 4.0 and p["slippage_bps_other"] == 10.0
    assert "current depth, not historical depth" in rep["slippage_note"]


def test_missing_book_falls_back_and_is_labelled():
    store = Store(":memory:")
    seed(store, "ETH-USD", 0.0, 0.00001)
    rep = r.build_report(store, Settings(), [SYMBOL_MAP["ETH-USD"]], NOW)
    used = rep["markets"][0]["slippage_used"]
    assert used[STRIKE]["bps"] == 2.0 and "unavailable" in used[STRIKE]["source"]


def test_extreme_funding_pair_flagged_and_excluded_from_summary():
    store = Store(":memory:")
    seed(store, "ETH-USD", 0.0, 0.00001)  # carry 0.1/h -> 72 - 27 = +45: normal
    seed(store, "SOL-USD", 0.0, 0.0002)  # carry 2/h -> 1440 - 27 = +1413: extreme (> $500)
    rep = r.build_report(store, Settings(), [SYMBOL_MAP["ETH-USD"], SYMBOL_MAP["SOL-USD"]], NOW)
    by = {(m["symbol"], p["direction"]): p for m in rep["markets"] for p in m["pairs"]}
    eth_long = by[("ETH-USD", r.LONG_STRIKE)]
    sol_long = by[("SOL-USD", r.LONG_STRIKE)]
    assert not eth_long["low_confidence"]
    assert sol_long["low_confidence"] and sol_long["low_confidence_reason"] == r.LOW_CONF_REASON
    # mirror direction is -1467, also extreme: the guard is on |net|
    assert by[("SOL-USD", r.LONG_OTHER)]["low_confidence"]
    lines = rep["summary"]
    pair_lines = [x for x in lines if x.startswith(("Best pair", "Worst pair"))]
    assert pair_lines and all("ETH" in x for x in pair_lines)  # SOL pairs left out
    assert any(x.startswith("Of 2 market and direction pairs") for x in lines)
    assert any(x.startswith("2 pairs flagged as low confidence") for x in lines)
    assert sol_long["cum_net_30d_usd"] == pytest.approx(0.0002 * 10_000 * 720 - 27)  # still listed


def test_thin_venue_flags_pair_even_with_normal_funding():
    store = Store(":memory:")
    seed(store, "ETH-USD", 0.0, 0.00001)
    thin = r.SlippageEstimate(2.0, "fallback: book too thin for notional", None, thin=True)
    rep = r.build_report(
        store, Settings(), [SYMBOL_MAP["ETH-USD"]], NOW, slippage={(HYPERLIQUID, "ETH-USD"): thin}
    )
    assert all(p["low_confidence"] for p in rep["markets"][0]["pairs"])
    # the headline sentence for a thin venue is dropped
    assert not any("ETH longs vs Hyperliquid" in s for s in rep["summary"])


# --- rate-limit header tracking ---------------------------------------------------------
async def test_rate_headers_are_recorded_if_present(ft):
    def handler(req):
        return httpx.Response(200, json={}, headers={"x-mbx-used-weight-1m": "37", "etag": "x"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = UpstreamClient("t", TokenBucket(10, 10, clock=ft.clock, sleep=ft.sleep), http,
                       clock=ft.clock, sleep=ft.sleep)  # fmt: skip
    await c.get("http://x/y")
    assert c.rate_headers == {"x-mbx-used-weight-1m": "37"}
