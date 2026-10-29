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


# --- bootstrap backfill (TVL, share price, volume) ---------------------------------
DAY = 86_400_000


def test_parse_slp_history_rows(fx):
    rows = strike_parse().parse_slp_history(fx("strike_vault_portfolio_slp.json"))
    assert len(rows) == 80 and rows == sorted(rows)
    ts, tvl, sp = rows[0]
    assert (ts, tvl, sp) == (
        1792911600000,
        pytest.approx(1049714.89),
        pytest.approx(1.17217, abs=1e-5),
    )


def test_parse_slp_history_skips_bad_rows_and_tolerates_short_rows():
    data = {
        "history": [
            [2000, "5.5"],
            [1000, None, "1", "0", "3", "1.0"],
            [3000, "7", "0", "0", "1", "1.2"],
        ]
    }
    assert strike_parse().parse_slp_history(data) == [(2000, 5.5, None), (3000, 7.0, 1.2)]
    assert strike_parse().parse_slp_history({}) == []


def test_parse_daily_volume_sums_symbols_per_day(fx):
    vol = strike_parse().parse_daily_volume(fx("strike_dashboard_volumes.json"))
    assert len(vol) == 6 and all(v > 0 for v in vol.values())
    assert strike_parse().parse_daily_volume(
        {"total_volume_daily": [[DAY, "A", 1.5], [DAY, "B", "2.5"], [DAY, "C", None]]}
    ) == {DAY: 4.0}


def strike_parse():
    from app.adapters import strike

    return strike


class FakeSource:
    def __init__(self, hourly, daily, volumes, fail=()):
        self.data = {"30d": hourly, "6m": daily}
        self.volumes, self.fail, self.calls = volumes, set(fail), []

    async def fetch_slp_history(self, period):
        self.calls.append(period)
        if period in self.fail:
            raise UpstreamError(f"{period} down")
        return self.data[period]

    async def fetch_daily_volume(self):
        self.calls.append("volume")
        if "volume" in self.fail:
            raise UpstreamError("volume down")
        return self.volumes


def backfiller(store, src, now):
    from app.backfill import Backfiller

    return Backfiller(store, {STRIKE: src}, [], now_ms=lambda: now)


BF_NOW = 100 * DAY + 5 * 3_600_000  # 05:00 UTC


def bf_source(**kw):
    midnight = 100 * DAY
    hourly = [
        (midnight - 2 * 3_600_000 + h * 3_600_000, 1000.0 + h, 1.0 + h / 1000) for h in range(8)
    ]
    # hourly rows: 22:00, 23:00, 00:00 (a midnight), 01:00 ... 05:00
    daily = [
        (midnight - 80 * DAY, 500.0, 0.9),
        (midnight - 3 * DAY, 800.0, 0.99),
        (midnight, 999.0, 1.0),
    ]
    volumes = {midnight - DAY: 7_000_000.0, midnight - 2 * DAY: 6_000_000.0, midnight: 1_000.0}
    return FakeSource(hourly, daily, volumes, **kw)


async def test_backfill_writes_tvl_share_price_and_volume_on_midnights():
    store = Store(":memory:")
    n = await backfiller(store, bf_source(), BF_NOW).run_liquidity()
    rows = {r.ts_ms: r for r in store.get_liquidity(0)}
    midnight = 100 * DAY
    assert n == len(rows) == 9  # 8 hourly + 1 older daily (midnight is in both); 80d old is out
    assert midnight - 80 * DAY not in rows
    assert rows[midnight].volume_24h_usd == 7_000_000.0  # day 99's total, as of midnight
    assert rows[midnight - 3 * DAY].volume_24h_usd is None  # no volume known for that day
    assert rows[midnight + 3_600_000].volume_24h_usd is None  # volume only on midnights
    assert rows[midnight].slp_tvl_usd == 1002.0 and rows[midnight].slp_share_price == pytest.approx(
        1.002
    )
    assert all(r.debt_total_usd is None and r.debt_by_asset is None for r in rows.values())


async def test_backfill_never_overwrites_live_values_and_only_fills_nulls():
    store = Store(":memory:")
    midnight = 100 * DAY
    store.save_liquidity(LiquiditySample(midnight, 5.0, 2.0, 77.0, {"ADA": 77.0}, None))
    await backfiller(store, bf_source(), BF_NOW).run_liquidity()
    row = {r.ts_ms: r for r in store.get_liquidity(0)}[midnight]
    assert (row.slp_tvl_usd, row.slp_share_price, row.debt_total_usd) == (5.0, 2.0, 77.0)
    assert row.volume_24h_usd == 7_000_000.0  # the missing volume was filled


async def test_backfill_is_throttled_then_reruns_after_the_refresh_interval():
    store = Store(":memory:")
    src = bf_source()
    bf = backfiller(store, src, BF_NOW)
    assert await bf.run_liquidity() > 0
    calls = len(src.calls)
    assert await bf.run_liquidity() == 0 and len(src.calls) == calls  # checked moments ago
    bf.now_ms = lambda: BF_NOW + 7 * 3_600_000
    await bf.run_liquidity()
    assert len(src.calls) == 2 * calls
    assert store.get_state(STRIKE, "liquidity").status == "ok"


async def test_backfill_volume_failure_still_writes_tvl_and_retries_volume_later():
    store = Store(":memory:")
    bf = backfiller(store, bf_source(fail=["volume"]), BF_NOW)
    assert await bf.run_liquidity() == 9
    assert all(r.volume_24h_usd is None for r in store.get_liquidity(0))
    bf.sources[STRIKE] = bf_source()
    bf.now_ms = lambda: BF_NOW + 7 * 3_600_000
    await bf.run_liquidity()
    assert {r.ts_ms: r for r in store.get_liquidity(0)}[100 * DAY].volume_24h_usd == 7_000_000.0


async def test_backfill_one_history_period_failing_keeps_the_other():
    store = Store(":memory:")
    assert await backfiller(store, bf_source(fail=["6m"]), BF_NOW).run_liquidity() == 8
    store2 = Store(":memory:")
    assert await backfiller(store2, bf_source(fail=["30d"]), BF_NOW).run_liquidity() == 2


async def test_backfill_total_failure_records_error_and_writes_nothing():
    store = Store(":memory:")
    bf = backfiller(store, bf_source(fail=["30d", "6m"]), BF_NOW)
    assert await bf.run_liquidity() == 0 and store.get_liquidity(0) == []
    st = store.get_state(STRIKE, "liquidity")
    assert st.status == "error" and "down" in st.error
    assert await bf.run_liquidity() == 0  # an error never throttles: retried next pass


async def test_backfill_skips_sources_without_liquidity_support():
    class Plain:
        pass

    assert await backfiller(Store(":memory:"), Plain(), BF_NOW).run_liquidity() == 0


def test_store_thinned_read_keeps_one_row_per_hour_before_the_cutoff():
    st = Store(":memory:")
    hour = 3_600_000
    for m in range(0, 180, 10):  # three hours of 10 minute samples
        st.save_liquidity(LiquiditySample(10 * hour + m * 60_000, 1.0, 1.0, 1.0, {}))
    cutoff = 12 * hour
    thin = st.get_liquidity(0, thin_before_ms=cutoff)
    old = [s.ts_ms for s in thin if s.ts_ms < cutoff]
    assert old == [10 * hour + 50 * 60_000, 11 * hour + 50 * 60_000]  # last sample of each hour
    assert [s.ts_ms for s in thin if s.ts_ms >= cutoff] == [
        s.ts_ms for s in st.get_liquidity(cutoff)
    ]  # recent rows untouched
