import pytest
from fastapi.testclient import TestClient

from app import schemas
from app.config import HYPERLIQUID, OKX, STRIKE, Settings
from app.db import Store
from app.main import create_app
from app.models import Funding
from tests.helpers import WED_NOON_UTC, build_state, seed_history

NOW = WED_NOON_UTC
V1 = (
    "/api/v1/snapshot",
    "/api/v1/opportunities",
    "/api/v1/holding-cost?symbol=BTC-USD&side=long",
    "/api/v1/report",
    "/api/v1/history?symbol=BTC-USD&days=30",
)


def make_app(state=None, now=NOW, **kw):
    store = Store(":memory:")
    seed_history(store, now)
    state = state or build_state(now)
    kw.setdefault("cache_ttl_seconds", 0)
    kw.setdefault("rate_limit_per_minute", 10_000)
    app = create_app(
        Settings(), store=store, state=state, now_ms=lambda: now, start_background=False, **kw
    )
    return app, state, store


@pytest.fixture
def client():
    app, _, _ = make_app()
    return TestClient(app)


# --- schemas, units, timestamps --------------------
@pytest.mark.parametrize("path", V1)
def test_every_v1_endpoint_returns_schema_units_and_timestamps(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == schemas.SCHEMA_VERSION
    assert body["generated_at_ms"] == NOW and body["generated_at_iso"].endswith("Z")
    assert body["disclaimer"].startswith("Estimates only")
    for v in (STRIKE, HYPERLIQUID, OKX):
        vs = body["venues"][v]
        assert vs["source_ts_ms"] and vs["source_ts_iso"].endswith("Z") and vs["stale"] is False
    a = body["assumptions"]
    assert a["notional_usd"] and a["hours"] and a["fee_mode"] and a["slippage_model"]
    assert set(a["fee_rates"]) == {"strike", "hyperliquid", "okx"}
    model = {
        "snapshot": schemas.SnapshotResponse,
        "opportunities": schemas.OpportunitiesResponse,
        "holding-cost": schemas.HoldingCostResponse,
        "report": schemas.ReportResponse,
        "history": schemas.HistoryResponse,
    }[path.split("?")[0].rsplit("/", 1)[1]]
    model.model_validate(body)  # exact schema match


def test_snapshot_contents_and_funding_display_fields(client):
    body = client.get("/api/v1/snapshot?notional=10000&hours=24").json()
    assert len(body["markets"]) == 31
    btc = next(m for m in body["markets"] if m["symbol"] == "BTC-USD")
    okx_q = btc["venues"]["okx"]
    assert okx_q["funding_interval_hours"] == 8.0
    assert okx_q["funding_rate_per_hour"] == pytest.approx(okx_q["funding_rate_per_interval"] / 8)
    assert okx_q["funding_paid_by_long_usd"] == pytest.approx(
        okx_q["funding_rate_per_hour"] * 10_000 * 24
    )
    assert okx_q["funding_apr"] == pytest.approx(okx_q["funding_rate_per_hour"] * 8760)
    assert okx_q["liquidity_ok"] is True and okx_q["buy_slippage_bps"] > 0
    tsla = next(m for m in body["markets"] if m["symbol"] == "TSLA-USD")
    assert tsla["is_tradfi"] and tsla["venues"]["hyperliquid"]["hl_fee_unverified"] is True
    assert btc["is_tradfi"] is False and btc["venues"]["hyperliquid"]["hl_fee_unverified"] is False
    assert btc["trade_url"] == "https://v2.strikefinance.org"


def test_trade_link_uses_referral_code():
    store = Store(":memory:")
    app = create_app(
        Settings(strike_referral_code="XYZ"), store=store, state=build_state(),
        now_ms=lambda: NOW, start_background=False, cache_ttl_seconds=0,
    )  # fmt: skip
    body = TestClient(app).get("/api/v1/opportunities?symbol=BTC").json()
    assert (
        body["opportunities"][0]["trade_url"]
        == "https://v2.strikefinance.org/referral/accept?code=XYZ"
    )


# --- opportunities --------------------
def test_opportunities_ranked_by_net_descending_with_ranks(client):
    body = client.get("/api/v1/opportunities?hours=168").json()
    nets = [o["net_usd"] for o in body["opportunities"]]
    assert nets == sorted(nets, reverse=True) and body["count"] == len(nets) > 20
    assert [o["rank"] for o in body["opportunities"]] == list(range(1, len(nets) + 1))
    top = body["opportunities"][0]
    assert top["net_usd_by_period"].keys() == {"1d", "1w", "1m"}
    assert top["line_items"][-1]["label"] == "Estimated net"
    assert top["net_usd"] == pytest.approx(top["net_usd_by_period"]["1w"], rel=1e-9)


def test_period_preset_math_matches_carry_and_costs(client):
    o = client.get("/api/v1/opportunities?symbol=ETH-USD&direction=long_strike_short_other").json()
    row = o["opportunities"][0]
    carry, cost = row["carry_usd_per_hour"], row["round_trip_cost_usd"]
    for k, h in (("1d", 24), ("1w", 168), ("1m", 720)):
        assert row["net_usd_by_period"][k] == pytest.approx(carry * h - cost)


def test_two_leg_row_matches_hand_calc_from_snapshot(client):
    snap = client.get("/api/v1/snapshot").json()
    eth = next(m for m in snap["markets"] if m["symbol"] == "ETH-USD")["venues"]
    opp = client.get(
        "/api/v1/opportunities?symbol=ETH-USD&direction=long_strike_short_other&notional=10000&hours=24"
    ).json()["opportunities"]
    row = next(o for o in opp if o["short_venue"] == "okx")
    carry = 10_000 * (eth["okx"]["funding_rate_per_hour"] - eth["strike"]["funding_rate_per_hour"])
    assert row["carry_usd_per_hour"] == pytest.approx(carry)
    fees = 2 * 10_000 * (0.0005 + 0.0005)
    assert row["fees_usd"] == pytest.approx(fees)
    slip = 10_000 * sum(
        (eth[v]["buy_slippage_bps"] + eth[v]["sell_slippage_bps"]) / 1e4 for v in ("strike", "okx")
    )
    assert row["slippage_usd"] == pytest.approx(slip, rel=1e-3)
    assert row["net_usd"] == pytest.approx(carry * 24 - fees - slip, rel=1e-3)


def test_direction_filter_symbol_filter_and_min_net(client):
    d = client.get("/api/v1/opportunities?direction=long_other_short_strike").json()
    assert d["opportunities"] and all(o["short_venue"] == "strike" for o in d["opportunities"])
    s = client.get("/api/v1/opportunities?symbol=btc").json()
    assert {o["symbol"] for o in s["opportunities"]} == {"BTC-USD"}
    allrows = client.get("/api/v1/opportunities").json()["opportunities"]
    cut = allrows[3]["net_usd"]
    m = client.get(f"/api/v1/opportunities?min_net_usd={cut}").json()
    assert m["count"] == 4 and all(o["net_usd"] >= cut for o in m["opportunities"])


def test_ticker_only_excluded_by_default_and_included_with_flag(client):
    ticker = {"CXMT-USD", "DRAM-USD", "MINIMAX-USD", "SPCX-USD", "UNITREE-USD", "ZHIPU-USD"}
    d = client.get("/api/v1/opportunities").json()
    assert not ticker & {o["symbol"] for o in d["opportunities"]}
    ex = [o for o in d["unranked"] if o["symbol"] in ticker]
    assert ex and all("ticker match only" in o["exclusion_reason"] for o in ex)
    assert all(o["confidence"] == "Low" and o["ticker_only"] for o in ex)
    inc = client.get("/api/v1/opportunities?include_unverified=true").json()
    got = {o["symbol"] for o in inc["opportunities"]}
    assert ticker <= got
    assert all(o["confidence"] == "Low" for o in inc["opportunities"] if o["symbol"] in ticker)


def test_hip3_rows_lose_a_confidence_level_and_say_why(client):
    rows = client.get("/api/v1/opportunities?symbol=TSLA-USD").json()["opportunities"]
    hl_rows = [o for o in rows if "hyperliquid" in (o["long_venue"], o["short_venue"])]
    assert hl_rows and all(o["hl_fee_unverified"] for o in hl_rows)
    assert all(o["confidence"] != "High" for o in hl_rows)
    assert all(
        any(
            "HL fee unverified (HIP-3 deployer fee not exposed)" in r
            for r in o["confidence_reasons"]
        )
        for o in hl_rows
    )
    btc = client.get("/api/v1/opportunities?symbol=BTC-USD").json()["opportunities"]
    assert not any(o["hl_fee_unverified"] for o in btc)


def test_market_closed_forces_low_with_reason():
    sat = 1792596000000 + 3 * 86_400_000  # a Saturday
    from datetime import UTC, datetime

    sat = int(datetime(2026, 10, 24, 15, 30, tzinfo=UTC).timestamp() * 1000)
    app, _, _ = make_app(state=build_state(sat), now=sat)
    rows = TestClient(app).get("/api/v1/opportunities?symbol=NVDA-USD").json()["opportunities"]
    assert rows and all(o["confidence"] == "Low" and not o["market_open"] for o in rows)
    assert all(any("Underlying market closed" in r for r in o["confidence_reasons"]) for o in rows)
    btc = TestClient(app).get("/api/v1/opportunities?symbol=BTC-USD").json()["opportunities"]
    assert all(o["market_open"] for o in btc)


# --- stale handling, nulls --------------------
def test_stale_venue_flagged_and_excluded_from_rankings():
    state = build_state(NOW)
    old = NOW - 6 * 60_000
    from dataclasses import replace

    state.quotes[OKX] = {s: replace(q, source_ts_ms=old) for s, q in state.quotes[OKX].items()}
    for k in [k for k in state.books if k[0] == OKX]:
        state.books[k] = replace(state.books[k], source_ts_ms=old)
    app, _, _ = make_app(state=state)
    c = TestClient(app)
    body = c.get("/api/v1/opportunities?symbol=BTC-USD").json()
    assert body["venues"]["okx"]["stale"] is True and body["venues"]["strike"]["stale"] is False
    assert all("okx" not in (o["long_venue"], o["short_venue"]) for o in body["opportunities"])
    stale_rows = [o for o in body["unranked"] if o["stale"]]
    assert stale_rows and all(o["stale_venues"] == ["okx"] for o in stale_rows)
    assert all(o["exclusion_reason"] == "stale data" and o["rank"] is None for o in stale_rows)
    hc = c.get("/api/v1/holding-cost?symbol=BTC-USD&side=long").json()
    okx_row = next(r for r in hc["venues_ranked"] if r["venue"] == "okx")
    assert okx_row["stale"] and not okx_row["ranked"]
    assert hc["best_venue"] in ("strike", "hyperliquid")


def test_missing_funding_is_null_not_zero():
    state = build_state(NOW)
    from dataclasses import replace

    q = state.quotes[OKX]["BTC-USD"]
    state.quotes[OKX]["BTC-USD"] = replace(q, funding=None)
    app, _, _ = make_app(state=state)
    c = TestClient(app)
    snap = c.get("/api/v1/snapshot").json()
    btc = next(m for m in snap["markets"] if m["symbol"] == "BTC-USD")["venues"]["okx"]
    for f in ("funding_rate_per_interval", "funding_rate_per_hour", "funding_paid_by_long_usd",
              "funding_apr"):  # fmt: skip
        assert btc[f] is None
    opps = c.get("/api/v1/opportunities?symbol=BTC-USD").json()
    okx_rows = [o for o in opps["unranked"] if "okx" in (o["long_venue"], o["short_venue"])]
    assert okx_rows and all(
        o["net_usd"] is None and o["carry_usd_per_hour"] is None for o in okx_rows
    )
    assert all(o["exclusion_reason"] == "missing funding data" for o in okx_rows)


def test_zero_funding_is_reported_as_zero_not_null():
    state = build_state(NOW)
    from dataclasses import replace

    q = state.quotes[OKX]["BTC-USD"]
    state.quotes[OKX]["BTC-USD"] = replace(q, funding=Funding(0.0, 8.0))
    app, _, _ = make_app(state=state)
    snap = TestClient(app).get("/api/v1/snapshot").json()
    btc = next(m for m in snap["markets"] if m["symbol"] == "BTC-USD")["venues"]["okx"]
    assert btc["funding_rate_per_hour"] == 0.0 and btc["funding_paid_by_long_usd"] == 0.0


def test_thin_book_marks_insufficient_liquidity_and_excludes():
    from tests.helpers import synthetic_book

    state = build_state(NOW)
    state.books[(OKX, "BTC-USD")] = synthetic_book(84000, NOW, depth_usd=3_000)
    app, _, _ = make_app(state=state)
    body = TestClient(app).get("/api/v1/opportunities?symbol=BTC-USD").json()
    thin = [o for o in body["unranked"] if "okx" in (o["long_venue"], o["short_venue"])]
    assert thin and all(o["exclusion_reason"] == "insufficient liquidity" for o in thin)
    assert all(o["liquidity_ok"] is False and o["net_usd"] is None for o in thin)


# --- holding cost / direction toggle --------------------
def test_holding_cost_best_worst_and_gap(client):
    for side in ("long", "short"):
        hc = client.get(f"/api/v1/holding-cost?symbol=ETH-USD&side={side}&hours=168").json()
        costs = {r["venue"]: r["total_cost_usd"] for r in hc["venues_ranked"] if r["ranked"]}
        assert hc["best_venue"] == min(costs, key=costs.get)
        assert hc["worst_venue"] == max(costs, key=costs.get)
        assert hc["best_vs_worst_gap_usd"] == pytest.approx(
            max(costs.values()) - min(costs.values())
        )
        r = hc["venues_ranked"][0]
        total = (
            r["entry_fee_usd"] + r["exit_fee_usd"] + r["entry_slippage_usd"]
            + r["exit_slippage_usd"] + r["funding_cost_usd"]
        )  # fmt: skip
        assert r["total_cost_usd"] == pytest.approx(total)


def test_holding_cost_long_and_short_funding_have_opposite_signs(client):
    lo = client.get("/api/v1/holding-cost?symbol=BTC-USD&side=long").json()["venues_ranked"]
    sh = client.get("/api/v1/holding-cost?symbol=BTC-USD&side=short").json()["venues_ranked"]
    lf = {r["venue"]: r["funding_cost_usd"] for r in lo}
    sf = {r["venue"]: r["funding_cost_usd"] for r in sh}
    for v in lf:
        assert lf[v] == pytest.approx(-sf[v])


def test_maker_mode_uses_maker_fees_and_zero_slippage(client):
    hc = client.get("/api/v1/holding-cost?symbol=BTC-USD&side=long&fee_mode=maker").json()
    s = next(r for r in hc["venues_ranked"] if r["venue"] == "strike")
    assert s["fee_rate"] == pytest.approx(-0.00005)
    assert s["entry_fee_usd"] == pytest.approx(-0.5) and s["entry_slippage_usd"] == 0.0
    assert hc["assumptions"]["fee_mode"] == "maker"


# --- validation and errors --------------------
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/opportunities?notional=-5",
        "/api/v1/opportunities?hours=0",
        "/api/v1/opportunities?fee_mode=bogus",
        "/api/v1/opportunities?direction=sideways",
        "/api/v1/holding-cost?symbol=BTC-USD",
        "/api/v1/holding-cost?symbol=BTC-USD&side=flat",
        "/api/v1/history?symbol=BTC-USD&days=99",
        "/api/v1/snapshot?notional=abc",
    ],
)
def test_query_validation_uses_error_format(client, path):
    r = client.get(path)
    assert r.status_code == 422
    assert set(r.json()) == {"error", "detail"} and r.json()["error"] == "validation_error"


def test_unknown_symbol_is_404_error_format(client):
    r = client.get("/api/v1/holding-cost?symbol=DOGE-USD&side=long")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert set(r.json()) == {"error", "detail"}
    assert client.get("/api/v1/nope").json()["error"] == "not_found"


# --- history / report --------------------
def test_history_nulls_and_hourly_normalization(client):
    body = client.get("/api/v1/history?symbol=BTC-USD&days=2").json()
    assert body["unit"].startswith("fraction per hour")
    s = body["venues_history"]
    assert len(s["strike"]) == 48 and all(
        p["funding_rate_per_hour"] == 0.00001 for p in s["strike"]
    )
    assert all(
        p["funding_rate_per_hour"] == pytest.approx(0.0001)
        for p in s["okx"]
        if p["funding_rate_per_hour"] is not None
    )
    ndata = client.get("/api/v1/history?symbol=XAU-USD&days=1").json()["venues_history"]
    assert all(p["funding_rate_per_hour"] is None for p in ndata["strike"])  # no data: null


def test_report_endpoint_flags_loading_and_has_assumptions(client):
    body = client.get("/api/v1/report").json()
    assert body["history_loading"] is False and body["window_days"] == 30
    assert "Past funding does not predict future funding." in body["report_assumptions"]["notes"]
    btc = next(m for m in body["markets"] if m["symbol"] == "BTC-USD")
    assert btc["slippage_used"]["strike"]["source"]
    assert any("BTC" in line for line in body["summary"])


# --- cors, llms.txt, openapi --------------------
def test_cors_headers_for_get(client):
    r = client.get("/api/v1/snapshot", headers={"Origin": "https://bot.example"})
    assert r.headers["access-control-allow-origin"] == "*"
    pre = client.options(
        "/api/v1/snapshot",
        headers={"Origin": "https://x.example", "Access-Control-Request-Method": "GET"},
    )
    assert pre.status_code == 200 and "GET" in pre.headers["access-control-allow-methods"]
    assert "POST" not in pre.headers["access-control-allow-methods"]
    bad = client.options(
        "/api/v1/snapshot",
        headers={"Origin": "https://x.example", "Access-Control-Request-Method": "POST"},
    )
    assert bad.status_code == 400


def test_llms_txt_served(client):
    r = client.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    t = r.text
    for needle in ("/api/v1/opportunities", "/api/v1/holding-cost", "POSITIVE rate", "429",
                   "Retry-After", "stale", "Estimates only, not financial advice"):  # fmt: skip
        assert needle in t
    assert "—" not in t


def test_openapi_documents_every_endpoint_with_descriptions(client):
    spec = client.get("/openapi.json").json()
    assert {"/api/v1/snapshot", "/api/v1/opportunities", "/api/v1/holding-cost",
            "/api/v1/report", "/api/v1/history", "/healthz"} <= set(spec["paths"])  # fmt: skip
    comp = spec["components"]["schemas"]
    assert "Positive: longs pay shorts" in comp["VenueQuote"]["properties"][
        "funding_rate_per_interval"]["description"]  # fmt: skip
    assert "ErrorResponse" in comp
    assert client.get("/docs").status_code == 200
