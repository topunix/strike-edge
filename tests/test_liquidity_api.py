"""Liquidity quality through the JSON API, the MCP tool and the dashboard (all inputs mocked)."""

import json

import pytest
from mcp import Client

from app.config import LiquidityConfig, Settings
from app.db import Store
from app.mcp import build_mcp_server
from app.models import LiquiditySample
from tests.helpers import seed_history
from tests.test_web import NOW, make

HOUR, DAY = 3_600_000, 86_400_000


def seeded_store(*, debt_share, vol_ratio, days=8, with_debt=True):
    """Hourly samples ending at NOW; TVL 1,000,000, share price drifting up 0.01% per hour."""
    store = Store(":memory:")
    seed_history(store, NOW)
    n = days * 24
    for k in range(n, -1, -1):
        tvl = 1_000_000.0
        debt = debt_share(k) * tvl if with_debt else None
        store.save_liquidity(
            LiquiditySample(
                NOW - k * HOUR, tvl, 1.0 + 0.0001 * (n - k), debt,
                None if debt is None else {"sUSDrf": debt * 0.75, "ADA": debt * 0.25},
                vol_ratio(k) * tvl, 0.255,
            )
        )  # fmt: skip
    return store


def fragile_store():
    return seeded_store(debt_share=lambda k: 0.46 - 0.0005 * k, vol_ratio=lambda k: 10.0)


def get(client, path="/api/v1/liquidity-quality"):
    r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


SEVEN_DAY_APR = Settings(liquidity=LiquidityConfig(apr_window_days=7))


def test_endpoint_reports_metrics_trend_flag_and_series():
    _, c = make(settings=SEVEN_DAY_APR, store=fragile_store())
    body = get(c)
    cur = body["current"]
    assert cur["borrowed_share"] == pytest.approx(0.46)
    assert cur["volume_to_tvl"] == pytest.approx(10.0)
    assert cur["slp_tvl_usd"] == 1_000_000.0 and cur["debt_total_usd"] == pytest.approx(460_000)
    assert cur["debt_by_asset"]["sUSDrf"] == pytest.approx(345_000)
    assert cur["vault_apr"] == pytest.approx(0.255)  # Strike's published apr
    assert cur["derived_apr"] == pytest.approx((1.0192 / 1.0024 - 1) * 365 / 7)
    assert body["series_7d"][-1]["vault_apr"] == 0.255 and body["series_7d"][-1]["derived_apr"]
    assert body["trend_7d"]["borrowed_share"]["direction"] == "up"
    assert body["trend_7d"]["volume_to_tvl"]["direction"] == "flat"
    assert body["flag"]["status"] == "fragile"
    assert "Borrowed share of SLP is 46.0%" in body["flag"]["summary"]
    assert body["flag"]["thresholds"]["borrowed_share_high"] == 0.40
    assert len(body["series_7d"]) == 169 and 8 <= len(body["series_30d"]) <= 9
    assert body["series_7d"][-1]["borrowed_share"] == pytest.approx(0.46)
    assert body["debt_history_started_ms"] == NOW - 8 * DAY
    assert {"schema_version", "venues", "assumptions", "disclaimer"} <= set(body)


def test_endpoint_default_apr_window_is_30_days():
    body = get(make(store=fragile_store())[1])  # only 8 days of share prices
    assert body["current"]["derived_apr"] is None and body["current"]["vault_apr"] == 0.255
    long = seeded_store(debt_share=lambda k: 0.4, vol_ratio=lambda k: 10.0, days=35)
    assert get(make(store=long)[1])["current"]["derived_apr"] > 0


def test_endpoint_ok_when_volume_keeps_pace():
    store = seeded_store(
        debt_share=lambda k: 0.46 - 0.0005 * k,
        vol_ratio=lambda k: 10.0 + (168 - min(k, 168)) * 0.05,
    )
    body = get(make(store=store)[1])
    assert body["trend_7d"]["volume_to_tvl"]["direction"] == "up"
    assert body["flag"]["status"] == "ok"


def test_endpoint_thresholds_follow_settings():
    settings = Settings(liquidity=LiquidityConfig(borrowed_share_high=0.60))
    body = get(make(settings=settings, store=fragile_store())[1])
    assert (
        body["flag"]["status"] == "ok" and body["flag"]["thresholds"]["borrowed_share_high"] == 0.6
    )


def test_endpoint_without_any_samples_is_all_na_not_zero():
    _, c = make()
    body = get(c)
    assert all(v is None for v in body["current"].values())
    assert body["flag"]["status"] == "insufficient_data" and body["flag"]["summary"].startswith(
        "n/a"
    )
    assert body["series_7d"] == [] and body["debt_history_started_ms"] is None


def test_endpoint_with_young_debt_history_has_no_trend_and_no_flag():
    store = seeded_store(debt_share=lambda k: 0.50, vol_ratio=lambda k: 10.0, days=2)
    body = get(make(store=store)[1])
    assert body["current"]["borrowed_share"] == pytest.approx(0.5)
    assert body["trend_7d"]["borrowed_share"]["direction"] is None
    assert body["flag"]["status"] == "insufficient_data"
    assert (
        "Flag n/a" in body["flag"]["summary"]
        and "7 days of debt history" in body["flag"]["summary"]
    )


def test_endpoint_with_backfilled_tvl_only_never_invents_debt():
    store = seeded_store(debt_share=lambda k: 0.5, vol_ratio=lambda k: 10.0, with_debt=False)
    body = get(make(settings=SEVEN_DAY_APR, store=store)[1])
    assert body["current"]["borrowed_share"] is None and body["current"]["debt_total_usd"] is None
    assert body["current"]["volume_to_tvl"] == pytest.approx(10.0)  # independent input still shown
    assert body["current"]["derived_apr"] is not None
    assert body["flag"]["status"] == "insufficient_data"
    assert all(p["borrowed_share"] is None for p in body["series_7d"])


def test_endpoint_stale_samples_are_na():
    store = Store(":memory:")
    seed_history(store, NOW)
    store.save_liquidity(LiquiditySample(NOW - 30 * 60_000, 1e6, 1.0, 4e5, {"ADA": 4e5}, 1e7))
    body = get(make(store=store)[1])
    assert body["current"]["borrowed_share"] is None and body["current"]["slp_tvl_usd"] is None


def test_endpoint_is_documented_in_openapi():
    _, c = make()
    spec = c.get("/openapi.json").json()
    assert "/api/v1/liquidity-quality" in spec["paths"]


# --- MCP tool -------------------------------------------------------------------------
async def test_mcp_tool_returns_the_same_numbers_as_the_endpoint():
    app, c = make(store=fragile_store())
    server = build_mcp_server(app.state.ctx)
    async with Client(server) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["get_liquidity_quality"]
        result = await client.call_tool("get_liquidity_quality", {})
    assert tool.input_schema.get("required", []) == [] and tool.annotations.read_only_hint
    assert result.is_error is False
    assert json.loads(json.dumps(result.structured_content)) == get(c)
    assert result.structured_content["flag"]["status"] == "fragile"


# --- dashboard ------------------------------------------------------------------------
def test_dashboard_shows_share_trend_arrow_and_fragile_warning():
    _, c = make(store=fragile_store())
    html = c.get("/").text
    sec = html.split('aria-labelledby="liq-h"', 1)[1].split("</section>", 1)[0]
    assert "46.0%" in sec and "↑" in sec and "7d trend: rising" in sec
    assert "Fragile liquidity." in sec and "liq-warn" in sec
    assert "10.0x" in sec
    assert "published by Strike" in sec and "25.5%" in sec  # primary APR
    assert "derived from share price (30d): n/a" in sec  # secondary, labeled (8 days only)
    assert chr(0x2014) not in sec


def test_dashboard_shows_na_when_inputs_are_missing():
    _, c = make()
    sec = c.get("/").text.split('aria-labelledby="liq-h"', 1)[1].split("</section>", 1)[0]
    assert sec.count("n/a") >= 4 and "Fragile liquidity." not in sec
    assert "↑" not in sec and "↓" not in sec


def test_dashboard_no_warning_when_ok():
    store = seeded_store(debt_share=lambda k: 0.30, vol_ratio=lambda k: 10.0)
    sec = make(store=store)[1].get("/").text.split('aria-labelledby="liq-h"', 1)[1]
    sec = sec.split("</section>", 1)[0]
    assert "30.0%" in sec and "→" in sec and "Fragile liquidity." not in sec
