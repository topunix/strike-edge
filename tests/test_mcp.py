"""MCP server: tool schemas, parity with the v1 endpoints, rate limit, HTTP and stdio transports."""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters

from app.config import Settings
from app.db import Store
from app.main import create_app
from app.mcp import NOTICE, RESTRICTED_URL, build_mcp_server
from tests.browser_server import serve
from tests.helpers import WED_NOON_UTC, build_state, seed_history
from tests.test_web import make

ROOT = Path(__file__).resolve().parent.parent
TOOLS = {"get_opportunities", "get_holding_cost", "get_report"}
EM_DASH = chr(0x2014)  # built at runtime so this file has no literal dash


def roundtrip(x):
    """What a JSON consumer sees: dump and reload, so float and None handling match."""
    return json.loads(json.dumps(x))


def fixture_server():
    app, client = make()
    return app, client, build_mcp_server(app.state.ctx)


def flat(schema: dict) -> dict:
    """First non-null branch of an optional (anyOf) property schema."""
    if "anyOf" in schema:
        return next(s for s in schema["anyOf"] if s.get("type") != "null")
    return schema


# --- tool schemas and descriptions ----------------------------------------------------------
async def test_lists_exactly_three_read_only_tools_with_schemas():
    _, _, server = fixture_server()
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == TOOLS
    for t in tools.values():
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
        assert t.output_schema and t.input_schema["type"] == "object"

    opp = tools["get_opportunities"].input_schema
    assert set(opp["properties"]) == {
        "min_net_usd", "notional", "hours", "fee_mode", "direction", "symbol", "include_unverified",
    }  # fmt: skip
    assert opp.get("required", []) == []  # everything is optional
    notional = flat(opp["properties"]["notional"])
    assert notional["type"] == "number" and notional["exclusiveMinimum"] == 0
    assert notional["maximum"] == 100_000_000  # same bounds as the v1 query parameter
    assert flat(opp["properties"]["hours"])["maximum"] == 8760
    assert opp["properties"]["fee_mode"]["enum"] == ["taker", "maker"]
    assert flat(opp["properties"]["direction"])["enum"] == [
        "long_strike_short_other", "long_other_short_strike",
    ]  # fmt: skip
    assert opp["properties"]["include_unverified"]["default"] is False

    hold = tools["get_holding_cost"].input_schema
    assert set(hold["required"]) == {"symbol", "side"}
    assert hold["properties"]["side"]["enum"] == ["long", "short"]

    rep = tools["get_report"].input_schema
    assert set(rep["properties"]) == {"symbol"} and rep.get("required", []) == []


async def test_every_description_states_units_estimates_freshness_and_restricted_notice():
    _, _, server = fixture_server()
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        instructions = client.instructions
    for t in tools:
        d = t.description
        assert NOTICE in d, t.name
        for needle in ("UNITS", "*_usd is US dollars", "basis points", "per hour", "UTC epoch",
                       "positive rate means longs pay shorts", "null means missing",
                       "ESTIMATES ONLY", "not financial advice", "past funding does not predict",
                       "FRESHNESS", "5 minutes", "stale=true", "excluded from rankings",
                       "restricted jurisdictions", RESTRICTED_URL):  # fmt: skip
            assert needle in d, (t.name, needle)
    assert instructions and "read-only" in instructions and RESTRICTED_URL in instructions
    assert EM_DASH not in "".join(t.description for t in tools)


# --- parity with the v1 endpoints -----------------------------------------------------------------
V1_CASES = [
    ("get_opportunities", {}, "/api/v1/opportunities"),
    (
        "get_opportunities",
        {"symbol": "ETH", "hours": 168, "notional": 25000, "fee_mode": "maker",
         "direction": "long_strike_short_other", "min_net_usd": 0, "include_unverified": True},
        "/api/v1/opportunities?symbol=ETH&hours=168&notional=25000&fee_mode=maker"
        "&direction=long_strike_short_other&min_net_usd=0&include_unverified=true",
    ),
    ("get_opportunities", {"hours": 24}, "/api/v1/opportunities?hours=24"),
    ("get_holding_cost", {"symbol": "ETH-USD", "side": "long"}, "/api/v1/holding-cost?symbol=ETH-USD&side=long"),
    ("get_holding_cost", {"symbol": "btc", "side": "short", "hours": 168, "notional": 50000, "fee_mode": "maker"},
     "/api/v1/holding-cost?symbol=btc&side=short&hours=168&notional=50000&fee_mode=maker"),
    ("get_report", {}, "/api/v1/report"),
]  # fmt: skip


@pytest.mark.parametrize(("tool", "args", "path"), V1_CASES)
async def test_each_tool_returns_the_same_numbers_as_its_v1_endpoint(tool, args, path):
    _, client, server = fixture_server()
    v1 = client.get(path)
    assert v1.status_code == 200
    async with Client(server) as mcp_client:
        result = await mcp_client.call_tool(tool, args)
    assert result.is_error is False
    assert roundtrip(result.structured_content) == v1.json()  # identical, field for field
    assert json.loads(result.content[0].text) == v1.json()  # text content matches too


async def test_get_report_with_symbol_returns_that_markets_table_unchanged():
    _, client, server = fixture_server()
    full = client.get("/api/v1/report").json()
    async with Client(server) as mcp_client:
        one = (await mcp_client.call_tool("get_report", {"symbol": "btc"})).structured_content
    assert [m["symbol"] for m in one["markets"]] == ["BTC-USD"]
    assert one["markets"][0] == next(m for m in full["markets"] if m["symbol"] == "BTC-USD")
    assert one["summary"] == full["summary"] and one["key_findings"] == full["key_findings"]
    assert one["assumptions"] == full["assumptions"]


async def test_tools_validate_like_the_v1_endpoints():
    _, client, server = fixture_server()
    bad = [
        ("get_opportunities", {"notional": -1}, "/api/v1/opportunities?notional=-1"),
        ("get_opportunities", {"hours": 0}, "/api/v1/opportunities?hours=0"),
        ("get_opportunities", {"hours": 9000}, "/api/v1/opportunities?hours=9000"),
        ("get_opportunities", {"fee_mode": "bogus"}, "/api/v1/opportunities?fee_mode=bogus"),
        ("get_opportunities", {"direction": "sideways"}, "/api/v1/opportunities?direction=sideways"),
        ("get_holding_cost", {"symbol": "ETH-USD", "side": "flat"}, "/api/v1/holding-cost?symbol=ETH-USD&side=flat"),
        ("get_holding_cost", {"symbol": "ETH-USD"}, "/api/v1/holding-cost?symbol=ETH-USD"),
    ]  # fmt: skip
    async with Client(server) as mcp_client:
        for tool, args, path in bad:
            assert client.get(path).status_code == 422, path
            result = await mcp_client.call_tool(tool, args)
            assert result.is_error is True, (tool, args)
        # unknown symbol: 404 on v1, a tool error carrying the same message here
        v1 = client.get("/api/v1/holding-cost?symbol=NOPE&side=long")
        assert v1.status_code == 404
        err = await mcp_client.call_tool("get_holding_cost", {"symbol": "NOPE", "side": "long"})
        assert err.is_error is True and v1.json()["detail"] in err.content[0].text
        err = await mcp_client.call_tool("get_report", {"symbol": "NOPE"})
        assert err.is_error is True and "Unknown symbol" in err.content[0].text


async def test_stale_data_and_nulls_come_through_the_tools_unchanged():
    from dataclasses import replace

    state = build_state(WED_NOON_UTC)
    old = WED_NOON_UTC - 6 * 60_000
    state.quotes["okx"] = {s: replace(q, source_ts_ms=old) for s, q in state.quotes["okx"].items()}
    app, client = make(state=state)
    async with Client(build_mcp_server(app.state.ctx)) as mcp_client:
        got = (
            await mcp_client.call_tool("get_opportunities", {"symbol": "BTC-USD"})
        ).structured_content
    assert got["venues"]["okx"]["stale"] is True and got["venues"]["strike"]["stale"] is False
    assert all("okx" not in (o["long_venue"], o["short_venue"]) for o in got["opportunities"])
    assert roundtrip(got) == client.get("/api/v1/opportunities?symbol=BTC-USD").json()


# --- /mcp over HTTP, inside the FastAPI app ------------------------------------------------------------
async def test_mcp_endpoint_over_streamable_http_lists_and_calls_every_tool():
    app, client, _ = fixture_server()
    with serve(app) as base:
        async with Client(f"{base}/mcp") as mcp_client:
            tools = {t.name for t in (await mcp_client.list_tools()).tools}
            assert tools == TOOLS
            opp = await mcp_client.call_tool(
                "get_opportunities", {"symbol": "ETH-USD", "hours": 168}
            )
            hold = await mcp_client.call_tool(
                "get_holding_cost", {"symbol": "ETH-USD", "side": "long", "hours": 168}
            )
            rep = await mcp_client.call_tool("get_report", {"symbol": "ETH-USD"})
    v1_opp = client.get("/api/v1/opportunities?symbol=ETH-USD&hours=168").json()
    v1_hold = client.get("/api/v1/holding-cost?symbol=ETH-USD&side=long&hours=168").json()
    assert roundtrip(opp.structured_content) == v1_opp and opp.is_error is False
    assert roundtrip(hold.structured_content) == v1_hold and hold.is_error is False
    assert rep.is_error is False and rep.structured_content["markets"][0]["symbol"] == "ETH-USD"
    assert opp.structured_content["opportunities"] and hold.structured_content["best_venue"]


def mcp_post(client, method="tools/list", id_=1):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )


def test_rate_limit_applies_to_mcp_and_is_shared_with_the_api():
    store = Store(":memory:")
    seed_history(store, WED_NOON_UTC)
    app = create_app(
        Settings(), store=store, state=build_state(WED_NOON_UTC), now_ms=lambda: WED_NOON_UTC,
        start_background=False, cache_ttl_seconds=0, rate_limit_per_minute=3,
    )  # fmt: skip
    with TestClient(app) as c:
        first = mcp_post(c)
        assert first.status_code == 200 and first.json()["result"]["tools"]
        assert c.get("/api/v1/snapshot").status_code == 200  # same client, same budget
        assert mcp_post(c, id_=2).status_code == 200
        limited = mcp_post(c, id_=3)
        assert limited.status_code == 429
        assert limited.json()["error"] == "rate_limited" and set(limited.json()) == {
            "error",
            "detail",
        }
        assert int(limited.headers["retry-after"]) >= 1
        for _ in range(3):  # /healthz stays exempt
            assert c.get("/healthz").status_code == 200


def test_mcp_rate_limit_uses_x_forwarded_for_like_the_api():
    store = Store(":memory:")
    app = create_app(
        Settings(), store=store, state=build_state(WED_NOON_UTC), now_ms=lambda: WED_NOON_UTC,
        start_background=False, cache_ttl_seconds=0, rate_limit_per_minute=2,
    )  # fmt: skip
    a, b = {"X-Forwarded-For": "203.0.113.5"}, {"X-Forwarded-For": "198.51.100.6"}
    with TestClient(app) as c:
        for i in range(2):
            r = c.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": i, "method": "tools/list", "params": {}},
                headers={"Accept": "application/json, text/event-stream", **a},
            )
            assert r.status_code == 200
        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream", **a},
        )
        assert r.status_code == 429
        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream", **b},
        )
        assert r.status_code == 200


def test_mcp_route_does_not_break_json_404s_or_openapi():
    _, c = make()
    assert c.get("/definitely-not-here").json()["error"] == "not_found"
    assert c.get("/api/v1/nope").status_code == 404
    spec = c.get("/openapi.json").json()
    assert "/mcp" not in spec["paths"]  # transport endpoint, not part of the REST docs


# --- stdio entry point --------------------------------------------------------------------------------------
async def test_stdio_entry_point_starts_lists_tools_and_answers(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp", "--no-background", "--db-path", str(tmp_path / "mcp.db")],
        cwd=str(ROOT),
    )
    async with Client(params) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert tools == TOOLS
        res = await client.call_tool("get_opportunities", {"hours": 168})
        assert res.is_error is False
        got = res.structured_content
        # no poller in this mode: no data yet, and the response says so instead of inventing any
        assert got["opportunities"] == [] and got["count"] == 0
        assert all(v["stale"] for v in got["venues"].values() if v["enabled"])
        assert got["schema_version"] == "1.0" and got["disclaimer"].startswith("Estimates only")
        hold = await client.call_tool("get_holding_cost", {"symbol": "ETH", "side": "long"})
        assert hold.is_error is False and hold.structured_content["best_venue"] is None
        err = await client.call_tool("get_holding_cost", {"symbol": "NOPE", "side": "long"})
        assert err.is_error is True


def test_readme_and_demo_document_the_mcp_endpoint():
    readme = (ROOT / "README.md").read_text()
    assert "## Use with AI agents (MCP)" in readme
    assert readme.index("## Use with AI agents (MCP)") < readme.index(
        "## How existing tools compare"
    )
    for needle in (
        "https://strike-edge.onrender.com/mcp", "get_opportunities", "get_holding_cost", "get_report",
        "claude mcp add --transport http strike-edge", "python", "app.mcp", "Streamable HTTP",
        "read-only", "no write actions", "restricted-jurisdictions notice", "Example questions",
        "Where is ETH cheapest to hold long for 1 week at $10k, after fees?",
    ):  # fmt: skip
        assert needle in readme, needle
    questions = readme.split("**Example questions to ask an assistant:**")[1].split(
        "Free hosting note"
    )[0]
    assert len([ln for ln in questions.splitlines() if ln.strip()[:2] in ("1.", "2.", "3.")]) == 3

    demo = (ROOT / "docs" / "DEMO.md").read_text()
    assert "2:00 to 2:20" in demo and "an AI assistant over MCP" in demo
    assert "Where is ETH cheapest to hold long for 1 week at $10k, after fees?" in demo
    assert (
        "https://strike-edge.onrender.com/mcp" in demo
        and "Fallback if the connector is unavailable" in demo
    )
    assert "2 minute 20 second" in demo and "under the 2.5 minute cap" in demo
    ends = [
        tuple(map(int, m.split(":")))
        for m in __import__("re").findall(r"\d:\d\d to (\d:\d\d)", demo)
    ]
    assert max(ends) <= (2, 30)  # the whole run stays under 2.5 minutes
