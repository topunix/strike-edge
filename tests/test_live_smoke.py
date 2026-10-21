"""Live smoke test against the real upstream APIs. Skipped unless --live or RUN_LIVE=1."""

import pytest

from app import calc
from app.adapters import hyperliquid as hl
from app.adapters import okx
from app.adapters.base import UpstreamClient
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.okx import OkxAdapter
from app.adapters.strike import StrikeAdapter
from app.config import STRIKE, SYMBOL_MAP

pytestmark = pytest.mark.live


async def test_live_quotes_books_and_price_sanity():
    markets = list(SYMBOL_MAP.values())
    sc = UpstreamClient(STRIKE, StrikeAdapter.make_bucket())
    hc = UpstreamClient("hyperliquid", hl.make_bucket())
    oc = UpstreamClient("okx", okx.make_bucket(), endpoint_buckets=okx.make_buckets())
    try:
        s, h, o = StrikeAdapter(sc), HyperliquidAdapter(hc, markets), OkxAdapter(oc, markets)
        assert await s.load_rate_limits() == 2400
        sq, hq, oq = await s.fetch_quotes(), await h.fetch_quotes(), await o.fetch_quotes()
        assert len(sq) == 31 and len(hq) >= 25 and len(oq) >= 28
        for sym, q in hq.items():
            ratio = q.mark_price / sq[sym].mark_price
            assert 0.97 < ratio < 1.03, f"{sym} contract mismatch vs Strike: {ratio}"
        for sym, q in oq.items():
            ratio = q.mark_price / sq[sym].mark_price
            assert 0.97 < ratio < 1.03, f"{sym} OKX mismatch vs Strike: {ratio}"
        sb = await s.fetch_book("BTC-USD")
        hb = await h.fetch_book("BTC")
        ob = await o.fetch_book("BTC-USDT-SWAP")
        for b in (sb, hb, ob):
            f = calc.estimate_fills(b.bids, b.asks, 10_000)
            assert f.sufficient and f.buy_slippage_frac < 0.01
    finally:
        for c in (sc, hc, oc):
            await c.aclose()
