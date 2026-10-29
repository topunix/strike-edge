import pytest

from app.adapters.base import UpstreamError
from app.adapters.strike import StrikeAdapter
from app.config import STRIKE, SYMBOL_MAP, Settings
from app.db import Store
from app.models import LiquiditySample
from app.poller import LIQUIDITY_KEEP_MS, Poller
from app.state import LiveState

NOW = 1_792_596_600_000


class FakeClient:
    """Stands in for UpstreamClient: serves fixtures by URL, or raises for listed paths."""

    def __init__(self, fx, fail=()):
        self.fx, self.fail, self.calls = fx, tuple(fail), []

    async def get(self, url, **kw):
        self.calls.append((url, kw.get("params")))
        if any(f in url for f in self.fail):
            raise UpstreamError("HTTP 503")
        if "collateral-assets" in url:
            return self.fx("strike_credit_collateral_assets.json")
        if url.endswith("/v2/vaults"):
            return self.fx("strike_vaults_protocol.json")
        raise AssertionError(url)


async def test_fetch_liquidity_combines_debt_and_tvl(fx):
    client = FakeClient(fx)
    s = await StrikeAdapter(client).fetch_liquidity()
    assert s.debt_total_usd == pytest.approx(406_313.99, abs=0.01)
    assert s.debt_by_asset["sUSDrf"] == pytest.approx(304_546.71, abs=0.01)
    assert s.slp_tvl_usd == pytest.approx(930_974.02, abs=0.01) and s.slp_share_price
    assert s.volume_24h_usd is None
    assert ("https://api.strikefinance.org/v2/vaults", {"type": "protocol"}) in client.calls


async def test_fetch_liquidity_keeps_the_half_that_worked(fx):
    s = await StrikeAdapter(FakeClient(fx, fail=["collateral-assets"])).fetch_liquidity()
    assert s.debt_total_usd is None and s.debt_by_asset is None and s.slp_tvl_usd
    s = await StrikeAdapter(FakeClient(fx, fail=["/v2/vaults"])).fetch_liquidity()
    assert s.slp_tvl_usd is None and s.slp_share_price is None and s.debt_total_usd


async def test_fetch_liquidity_raises_when_both_fail(fx):
    with pytest.raises(UpstreamError):
        await StrikeAdapter(
            FakeClient(fx, fail=["collateral-assets", "/v2/vaults"])
        ).fetch_liquidity()


def test_store_liquidity_roundtrip_null_handling_and_prune():
    st = Store(":memory:")
    full = LiquiditySample(1000, 900.0, 1.1, 400.0, {"ADA": 100.0, "sUSDrf": 300.0}, 50.0)
    partial = LiquiditySample(2000, 900.0, 1.1, None, None)
    st.save_liquidity(full)
    st.save_liquidity(partial)
    assert st.get_liquidity(0) == [full, partial]
    assert st.get_liquidity(1500) == [partial]
    assert st.get_liquidity(0, 1500) == [full]
    assert st.prune_liquidity(1500) == 1 and st.get_liquidity(0) == [partial]


class SpyAdapter:
    def __init__(self, sample=None, error=None):
        self.sample, self.error, self.calls = sample, error, 0

    async def fetch_quotes(self):
        return {}

    async def fetch_book(self, native):
        raise AssertionError("no quotes, no books")

    async def fetch_liquidity(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.sample


def poller(adapter, store):
    async def no_sleep(_):
        return None

    return Poller(
        Settings(), LiveState(), store, {STRIKE: adapter}, list(SYMBOL_MAP.values())[:1],
        now_ms=lambda: NOW, sleep=no_sleep,
    )  # fmt: skip


async def test_poller_stores_a_sample_every_cycle():
    store = Store(":memory:")
    old = LiquiditySample(NOW - LIQUIDITY_KEEP_MS - 1, 1.0, 1.0, 1.0, {"ADA": 1.0})
    store.save_liquidity(old)
    sample = LiquiditySample(NOW, 900.0, 1.1, 400.0, {"ADA": 400.0})
    p = poller(SpyAdapter(sample), store)
    await p.run_cycle()
    assert store.get_liquidity(0) == [sample]  # stored, and the 45 day old row is pruned


async def test_poller_liquidity_failure_does_not_break_cycle_or_venue_health():
    store = Store(":memory:")
    p = poller(SpyAdapter(error=UpstreamError("HTTP 503")), store)
    await p.run_cycle()
    assert p.state.cycles == 1 and store.get_liquidity(0) == []
    assert STRIKE not in p.state.last_error  # liquidity gaps never mark the venue as erroring


async def test_poller_without_store_skips_liquidity():
    ad = SpyAdapter(LiquiditySample(NOW, 1.0, 1.0, 1.0, {}))
    await poller(ad, None).run_cycle()
    assert ad.calls == 0


async def test_poller_attaches_strike_volume_from_fresh_quotes():
    from tests.test_inbound_and_poller import quotes_for

    store = Store(":memory:")
    ad = SpyAdapter(LiquiditySample(NOW, 900.0, 1.1, 400.0, {"ADA": 400.0}))
    quotes = quotes_for(STRIKE)
    p = poller(ad, store)
    p.state.set_quotes(STRIKE, quotes, NOW)
    await p._liquidity_cycle()
    expected = sum(q.volume_24h_usd for q in quotes.values())
    assert expected > 0 and store.get_liquidity(0)[0].volume_24h_usd == pytest.approx(expected)


async def test_poller_volume_is_none_without_fresh_quotes():
    store = Store(":memory:")
    p = poller(SpyAdapter(LiquiditySample(NOW, 900.0, 1.1, 400.0, {"ADA": 400.0})), store)
    await p._liquidity_cycle()
    assert store.get_liquidity(0)[0].volume_24h_usd is None
