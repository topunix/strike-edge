import httpx
from fastapi.testclient import TestClient

from app.adapters.base import TokenBucket, UpstreamClient, UpstreamError
from app.config import HYPERLIQUID, OKX, STRIKE, SYMBOL_MAP, Settings
from app.db import Store
from app.main import create_app
from app.middleware import SlidingWindowLimiter
from app.models import Book
from app.poller import Poller
from app.state import LiveState
from tests.helpers import WED_NOON_UTC, build_state, seed_history, synthetic_book

NOW = WED_NOON_UTC


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def app_with(limit=3, ttl=0, clock=None, state=None):
    store = Store(":memory:")
    seed_history(store, NOW)
    return create_app(
        Settings(), store=store, state=state or build_state(NOW), now_ms=lambda: NOW,
        start_background=False, cache_ttl_seconds=ttl, rate_limit_per_minute=limit,
        limiter_clock=clock,
    )  # fmt: skip


# --- inbound limiter --------------------
def test_limiter_unit_window_and_retry_after():
    clk = Clock()
    lim = SlidingWindowLimiter(3, 60.0, clk)
    assert [lim.check("a")[0] for _ in range(3)] == [True, True, True]
    ok, retry = lim.check("a")
    assert not ok and retry == 60
    clk.t += 45
    ok, retry = lim.check("a")
    assert not ok and retry == 15
    clk.t += 16
    assert lim.check("a")[0]  # oldest hits aged out
    assert lim.check("b")[0]  # other clients unaffected


def test_inbound_429_with_retry_after_and_error_format():
    c = TestClient(app_with(limit=3, clock=Clock()))
    for _ in range(3):
        assert c.get("/api/v1/snapshot").status_code == 200
    r = c.get("/api/v1/snapshot")
    assert r.status_code == 429
    assert r.json()["error"] == "rate_limited" and "60" in r.json()["detail"]
    assert int(r.headers["retry-after"]) >= 1
    assert set(r.json()) == {"error", "detail"}


def test_inbound_uses_first_x_forwarded_for_entry():
    c = TestClient(app_with(limit=2, clock=Clock()))
    a = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
    b = {"X-Forwarded-For": "198.51.100.9, 10.0.0.1"}
    assert [c.get("/api/v1/snapshot", headers=a).status_code for _ in range(2)] == [200, 200]
    assert c.get("/api/v1/snapshot", headers=a).status_code == 429  # client a is limited
    assert c.get("/api/v1/snapshot", headers=b).status_code == 200  # client b has its own budget
    # same first entry, different proxy hops: still the same client
    a2 = {"X-Forwarded-For": "203.0.113.7, 10.9.9.9"}
    assert c.get("/api/v1/snapshot", headers=a2).status_code == 429


def test_inbound_falls_back_to_socket_address():
    c = TestClient(app_with(limit=1, clock=Clock()))
    assert c.get("/api/v1/snapshot").status_code == 200
    assert c.get("/api/v1/snapshot").status_code == 429  # testclient socket host is shared


def test_healthz_is_exempt_from_inbound_limit():
    c = TestClient(app_with(limit=1, clock=Clock()))
    assert c.get("/api/v1/snapshot").status_code == 200
    assert c.get("/api/v1/snapshot").status_code == 429
    for _ in range(5):
        assert c.get("/healthz").status_code == 200


# --- 30s cache ---------------------------------------------------------------------------------
def test_responses_cached_for_30s():
    c = TestClient(app_with(limit=100, ttl=30))
    r1 = c.get("/api/v1/opportunities?symbol=BTC-USD")
    r2 = c.get("/api/v1/opportunities?symbol=BTC-USD")
    r3 = c.get("/api/v1/opportunities?symbol=ETH-USD")
    assert r1.headers["x-cache"] == "MISS" and r2.headers["x-cache"] == "HIT"
    assert r3.headers["x-cache"] == "MISS"  # different query, different key
    assert r1.headers["cache-control"] == "public, max-age=30"
    assert r1.json() == r2.json()
    assert c.get("/healthz").headers.get("x-cache") is None  # health is never cached
    assert c.get("/api/v1/holding-cost?symbol=NOPE&side=long").headers.get("x-cache") is None


# --- healthz --------------------
def test_healthz_reports_per_venue_status_counts_and_degraded():
    app = app_with()
    ctx = app.state.ctx
    clk = [5000.0]
    client = UpstreamClient(OKX, TokenBucket(10, 10), httpx.AsyncClient(), clock=lambda: clk[0])
    client._count()
    client._count()
    client.degraded_until = 5100.0
    client.last_error = "HTTP 429; backing off 30.0s"
    ctx.clients[OKX] = client
    ctx.state.record_error(OKX, "HTTP 429", NOW)
    h = TestClient(app).get("/healthz").json()
    assert h["status"] == "degraded"
    assert h["venues"]["okx"]["status"] == "degraded" and h["venues"]["okx"]["degraded"] is True
    assert h["venues"]["okx"]["upstream_requests_last_minute"] == 2
    assert sum(h["venues"]["okx"]["upstream_requests_per_minute"].values()) == 2
    assert h["venues"]["okx"]["last_error"] == "HTTP 429"
    assert (
        h["venues"]["strike"]["status"] == "ok"
        and h["venues"]["strike"]["freshness_seconds"] == 10.0
    )
    assert h["inbound_rate_limit_per_minute"] == 60 or h["inbound_rate_limit_per_minute"] > 0


def test_healthz_stale_and_starting():
    empty = TestClient(app_with(state=LiveState())).get("/healthz").json()
    assert empty["status"] == "starting"
    assert empty["venues"]["strike"]["status"] == "starting"
    st = build_state(NOW)
    from dataclasses import replace

    st.quotes[HYPERLIQUID] = {
        s: replace(q, source_ts_ms=NOW - 10 * 60_000) for s, q in st.quotes[HYPERLIQUID].items()
    }
    h = TestClient(app_with(state=st)).get("/healthz").json()
    assert h["venues"]["hyperliquid"]["status"] == "stale" and h["status"] == "degraded"


def test_disabled_venue_is_reported_disabled():
    store = Store(":memory:")
    app = create_app(
        Settings(enabled_venues=(STRIKE, OKX)), store=store, state=build_state(NOW),
        now_ms=lambda: NOW, start_background=False, cache_ttl_seconds=0,
    )  # fmt: skip
    c = TestClient(app)
    assert c.get("/healthz").json()["venues"]["hyperliquid"]["status"] == "disabled"
    body = c.get("/api/v1/opportunities?symbol=BTC-USD").json()
    assert all(
        "hyperliquid" not in (o["long_venue"], o["short_venue"]) for o in body["opportunities"]
    )


# --- poller --------------------
class FakeAdapter:
    def __init__(self, venue, quotes, fail_quotes=False, fail_book_for=()):
        self.venue, self.quotes_ = venue, quotes
        self.fail_quotes, self.fail_book_for = fail_quotes, set(fail_book_for)
        self.book_calls: list[str] = []
        self.tradfi = {"TSLA-USD": True}

    async def fetch_quotes(self):
        if self.fail_quotes:
            raise UpstreamError("HTTP 503")
        return self.quotes_

    async def fetch_book(self, native):
        self.book_calls.append(native)
        if native in self.fail_book_for:
            raise UpstreamError("boom")
        return synthetic_book(100.0, NOW)


def poller_for(adapters, store=None, sleeps=None):
    st = LiveState()
    sl = sleeps if sleeps is not None else []

    async def fake_sleep(s):
        sl.append(s)

    markets = [SYMBOL_MAP[s] for s in ("BTC-USD", "ETH-USD", "SOL-USD")]
    return Poller(
        Settings(), st, store, adapters, markets, now_ms=lambda: NOW, sleep=fake_sleep,
        stagger_seconds=30.0,
    ), st, sl  # fmt: skip


def quotes_for(venue):
    full = build_state(NOW).quotes[venue]
    return {s: full[s] for s in ("BTC-USD", "ETH-USD", "SOL-USD")}


async def test_poller_updates_state_and_staggers_book_requests():
    ad = {
        STRIKE: FakeAdapter(STRIKE, quotes_for(STRIKE)),
        HYPERLIQUID: FakeAdapter(HYPERLIQUID, quotes_for(HYPERLIQUID)),
        OKX: FakeAdapter(OKX, quotes_for(OKX)),
    }
    p, st, sleeps = poller_for(ad)
    await p.run_cycle()
    assert set(st.quotes[STRIKE]) == {"BTC-USD", "ETH-USD", "SOL-USD"}
    assert ad[STRIKE].book_calls == ["BTC-USD", "ETH-USD", "SOL-USD"]
    assert ad[OKX].book_calls == ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
    assert ad[HYPERLIQUID].book_calls == ["BTC", "ETH", "SOL"]
    assert (STRIKE, "BTC-USD") in st.books and st.tradfi == {"TSLA-USD": True}
    # 3 books per venue, spaced 30/3 = 10s apart: 2 sleeps per venue, no bursting
    assert sleeps == [10.0] * 6
    assert st.cycles == 1 and st.last_cycle_ms == NOW


async def test_poller_isolates_venue_and_book_failures():
    ad = {
        STRIKE: FakeAdapter(STRIKE, quotes_for(STRIKE), fail_book_for={"ETH-USD"}),
        HYPERLIQUID: FakeAdapter(HYPERLIQUID, {}, fail_quotes=True),
        OKX: FakeAdapter(OKX, quotes_for(OKX)),
    }
    p, st, _ = poller_for(ad)
    await p.run_cycle()
    assert "HTTP 503" in st.last_error[HYPERLIQUID] and not st.quotes[HYPERLIQUID]
    assert ad[HYPERLIQUID].book_calls == []  # no book hammering without quotes
    assert st.quotes[OKX] and (OKX, "SOL-USD") in st.books
    assert (STRIKE, "BTC-USD") in st.books and (STRIKE, "ETH-USD") not in st.books
    assert (STRIKE, "SOL-USD") in st.books  # continued after the failed book
    assert "book ETH-USD" in st.last_error[STRIKE]


async def test_poller_respects_enabled_venues():
    ad = {
        STRIKE: FakeAdapter(STRIKE, quotes_for(STRIKE)),
        OKX: FakeAdapter(OKX, quotes_for(OKX)),
    }
    p, st, _ = poller_for(ad)
    p.adapters.pop(OKX)
    await p.run_cycle()
    assert ad[OKX].book_calls == [] and not st.quotes[OKX]


async def test_poller_saves_and_prunes_snapshots():
    store = Store(":memory:")
    ad = {
        STRIKE: FakeAdapter(STRIKE, quotes_for(STRIKE)),
        OKX: FakeAdapter(OKX, quotes_for(OKX)),
    }
    p, st, _ = poller_for(ad, store)
    store.save_snapshot(NOW - 8 * 24 * 3600 * 1000, {"old": True})
    await p.run_cycle()
    assert store.count_snapshots() == 1  # 8-day-old snapshot pruned, new one kept
    snap = store.get_snapshots(NOW - 1000)[0]
    assert snap["quotes"]["strike"]["BTC-USD"]["mark"] > 0
    assert snap["assumptions"]["notional_usd"] == 10_000.0
    assert snap["opportunities"] and snap["opportunities"][0]["rank"] == 1
    assert {"symbol", "net_usd", "confidence", "long_venue"} <= set(snap["opportunities"][0])


def test_snapshot_roundtrip_is_compressed_json():
    store = Store(":memory:")
    store.save_snapshot(5, {"a": [1, 2, 3], "n": None})
    store.save_snapshot(10, {"b": 1})
    assert store.get_snapshots(0) == [
        {"a": [1, 2, 3], "n": None, "ts_ms": 5},
        {"b": 1, "ts_ms": 10},
    ]
    assert store.get_snapshots(6) == [{"b": 1, "ts_ms": 10}]


def test_book_dataclass_mid():
    b = Book(((99.0, 1.0),), ((101.0, 1.0),), 1, 1)
    assert b.mid == 100.0 and Book((), (), 1, 1).mid is None
