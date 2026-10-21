import json

import httpx

from app.adapters import hyperliquid as hl
from app.adapters import okx, strike
from app.adapters.base import TokenBucket, UpstreamClient
from app.config import SYMBOL_MAP

HOUR = 3_600_000
T0 = 1_790_000_000_000 // HOUR * HOUR


def client(ft, handler, venue="test"):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return UpstreamClient(
        venue,
        TokenBucket(10_000, 10_000, clock=ft.clock, sleep=ft.sleep),
        http,
        clock=ft.clock,
        sleep=ft.sleep,
    )


def test_strike_history_parse_filters_range(fx):
    data = fx("strike_funding_history_BTC-USD.json")
    all_pts = strike.parse_funding_history(data, 0, 10**15)
    assert len(all_pts) == len(data["data"]) and all(p.interval_hours == 1.0 for p in all_pts)
    mid = all_pts[100].ts_ms
    sub = strike.parse_funding_history(data, mid, mid + 5 * HOUR)
    assert [p.ts_ms for p in sub] == [mid + i * HOUR for i in range(6)]


async def test_hyperliquid_history_paginates_past_500_rows(ft):
    rows = [
        {"coin": "BTC", "fundingRate": "0.0000125", "premium": "0", "time": T0 + i * HOUR + 41}
        for i in range(620)
    ]
    starts: list[int] = []

    def handler(req):
        body = json.loads(req.content)
        assert body["type"] == "fundingHistory"
        starts.append(body["startTime"])
        page = [r for r in rows if r["time"] >= body["startTime"]][:500]
        return httpx.Response(200, json=page)

    a = hl.HyperliquidAdapter(client(ft, handler), [SYMBOL_MAP["BTC-USD"]])
    pts = await a.fetch_funding_history(SYMBOL_MAP["BTC-USD"], T0, T0 + 700 * HOUR)
    assert len(pts) == 620 and len(starts) == 2
    assert starts[1] == rows[499]["time"] + 1  # advanced past the last row
    assert len({p.ts_ms for p in pts}) == 620


async def test_hyperliquid_history_exactly_500_rows_makes_second_call(ft):
    rows = [
        {"coin": "BTC", "fundingRate": "0.00001", "premium": "0", "time": T0 + i * HOUR}
        for i in range(500)
    ]
    n = 0

    def handler(req):
        nonlocal n
        n += 1
        body = json.loads(req.content)
        return httpx.Response(200, json=[r for r in rows if r["time"] >= body["startTime"]][:500])

    a = hl.HyperliquidAdapter(client(ft, handler), [SYMBOL_MAP["BTC-USD"]])
    pts = await a.fetch_funding_history(SYMBOL_MAP["BTC-USD"], T0, T0 + 600 * HOUR)
    assert len(pts) == 500 and n == 2  # second page empty, stops


async def test_okx_history_paginates_with_after_and_derives_interval(ft):
    # 150 settlements, 8h apart, newest first
    times = [T0 + i * 8 * HOUR for i in range(150)]
    afters: list[str | None] = []

    def handler(req):
        q = req.url.params
        afters.append(q.get("after"))
        cut = int(q["after"]) if q.get("after") else 10**18
        older = sorted((t for t in times if t < cut), reverse=True)[: int(q["limit"])]
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {"fundingTime": str(t), "fundingRate": "0.0001", "realizedRate": "0.0002"}
                    for t in older
                ],
            },
        )

    a = okx.OkxAdapter(client(ft, handler), [SYMBOL_MAP["BTC-USD"]])
    pts = await a.fetch_funding_history(SYMBOL_MAP["BTC-USD"], 0, 10**15)
    assert len(pts) == 150 and afters[0] is None and afters[1] == str(times[-100])
    assert all(p.interval_hours == 8.0 for p in pts)
    assert pts[0].rate_per_interval == 0.0002  # realizedRate preferred
    assert [p.ts_ms for p in pts] == sorted(times)


def test_okx_history_interval_change_and_single_row(fx):
    rows = [{"fundingTime": str(T0 + h * HOUR), "fundingRate": "0.001"} for h in (0, 8, 12, 16)]
    pts = okx.parse_funding_history(rows)
    assert [p.interval_hours for p in pts] == [8.0, 8.0, 4.0, 4.0]
    assert okx.parse_funding_history(rows[:1])[0].interval_hours == 8.0


def test_okx_history_fixture(fx):
    pts = okx.parse_funding_history(fx("okx_funding_history_BTC.json")["data"])
    assert len(pts) == 100 and all(p.interval_hours == 8.0 for p in pts)
