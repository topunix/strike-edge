from app.backfill import DAY_MS, Backfiller
from app.config import HYPERLIQUID, OKX, STRIKE, SYMBOL_MAP
from app.db import Store
from app.models import FundingPoint

HOUR = 3_600_000
NOW = 1_790_000_000_000 // HOUR * HOUR
MKTS = [SYMBOL_MAP["BTC-USD"], SYMBOL_MAP["NAS100-USD"], SYMBOL_MAP["ETH-USD"]]


class FakeSource:
    def __init__(self, interval=1.0, fail_on=()):
        self.calls: list[tuple[str, int, int]] = []
        self.interval = interval
        self.fail_on = set(fail_on)

    async def fetch_funding_history(self, market, start_ms, end_ms):
        self.calls.append((market.strike, start_ms, end_ms))
        if market.strike in self.fail_on:
            raise RuntimeError("boom")
        step = int(self.interval * HOUR)
        first = (start_ms + step - 1) // step * step
        return [
            FundingPoint(t, 0.0001 * self.interval, self.interval)
            for t in range(first, end_ms + 1, step)
        ]


def make(store, sources, now):
    return Backfiller(store, sources, MKTS, days=30, now_ms=lambda: now[0])


async def test_backfill_fills_cache_and_only_matched_markets():
    store, now = Store(":memory:"), [NOW]
    s, h, o = FakeSource(), FakeSource(), FakeSource(8.0)
    bf = make(store, {STRIKE: s, HYPERLIQUID: h, OKX: o}, now)
    assert bf.progress.loading
    p = await bf.run()
    assert not p.loading and p.failed == 0
    assert len(s.calls) == 3 and len(h.calls) == 2 and len(o.calls) == 3
    assert "NAS100-USD" not in {c[0] for c in h.calls}  # no Hyperliquid match
    pts = store.get_funding(STRIKE, "BTC-USD")
    assert len(pts) == 30 * 24 + 1 or len(pts) == 30 * 24


async def test_backfill_skips_cached_data_second_run():
    store, now = Store(":memory:"), [NOW]
    s = FakeSource()
    bf = make(store, {STRIKE: s}, now)
    await bf.run()
    calls = len(s.calls)
    now[0] += 10 * 60_000  # 10 minutes later: still fresh
    p = await bf.run()
    assert len(s.calls) == calls and p.skipped == 3


async def test_backfill_incremental_requests_only_new_range():
    store, now = Store(":memory:"), [NOW]
    s = FakeSource()
    bf = make(store, {STRIKE: s}, now)
    await bf.run()
    last = max(p.ts_ms for p in store.get_funding(STRIKE, "BTC-USD"))
    s.calls.clear()
    now[0] += 3 * HOUR
    await bf.run()
    btc = [c for c in s.calls if c[0] == "BTC-USD"][0]
    assert btc[1] == last + 1 and btc[2] == now[0]  # only the new range
    assert max(p.ts_ms for p in store.get_funding(STRIKE, "BTC-USD")) == now[0]


async def test_backfill_is_resumable_after_failure():
    store, now = Store(":memory:"), [NOW]
    bad = FakeSource(fail_on={"NAS100-USD"})
    bf = make(store, {STRIKE: bad}, now)
    p = await bf.run()
    assert p.failed == 1 and "strike:NAS100-USD" in p.errors
    assert store.get_state(STRIKE, "NAS100-USD").status == "error"
    assert store.get_state(STRIKE, "BTC-USD").status == "ok"
    good = FakeSource()
    bf2 = make(store, {STRIKE: good}, now)
    p2 = await bf2.run()
    # already-cached markets are skipped; only the failed one is fetched
    assert [c[0] for c in good.calls] == ["NAS100-USD"] and p2.skipped == 2 and p2.failed == 0


async def test_one_venue_failing_does_not_stop_others():
    store, now = Store(":memory:"), [NOW]
    bf = make(
        store,
        {STRIKE: FakeSource(), OKX: FakeSource(fail_on={"BTC-USD", "ETH-USD", "NAS100-USD"})},
        now,
    )
    p = await bf.run()
    assert p.failed == 3 and p.done == 3
    assert len(store.get_funding(STRIKE, "BTC-USD")) > 700


async def test_prunes_old_rows_and_window_is_30_days():
    store, now = Store(":memory:"), [NOW]
    store.upsert_funding(STRIKE, "BTC-USD", [FundingPoint(NOW - 60 * DAY_MS, 0.1, 1.0)])
    await make(store, {STRIKE: FakeSource()}, now).run()
    assert store.get_funding(STRIKE, "BTC-USD", NOW - 61 * DAY_MS)[0].ts_ms >= NOW - 31 * DAY_MS
