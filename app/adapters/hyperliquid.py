"""Hyperliquid adapter (main dex plus HIP-3 builder dexes such as "xyz").

Weights from the docs: l2Book 2, metaAndAssetCtxs 20 (all other info requests 20).
Limit: 1200 weight per minute per IP, we use 50%.
"""

from __future__ import annotations

import time

from app.adapters.base import TokenBucket, UpstreamClient
from app.config import HL_BASE, HYPERLIQUID, MarketMap
from app.models import Book, Funding, Quote

W_META, W_L2 = 20, 2
LIMIT_PER_MIN = 1200


def make_bucket(fraction: float = 0.5) -> TokenBucket:
    return TokenBucket.per_window(LIMIT_PER_MIN, 60.0, fraction)


def parse_quotes(meta_and_ctxs: list, wanted: dict[str, str], fetch_ts_ms: int) -> dict[str, Quote]:
    """Parse one metaAndAssetCtxs response.

    `wanted` maps HL coin name (e.g. "BTC" or "xyz:TSLA") -> Strike symbol.
    HL gives no source timestamp for this endpoint, so fetch time is used.
    Funding is hourly.
    """
    universe, ctxs = meta_and_ctxs[0]["universe"], meta_and_ctxs[1]
    out: dict[str, Quote] = {}
    for asset, ctx in zip(universe, ctxs, strict=False):
        name = asset["name"]
        if name not in wanted or asset.get("isDelisted"):
            continue
        funding = ctx.get("funding")
        mark = ctx.get("markPx")
        vol = ctx.get("dayNtlVlm")
        out[wanted[name]] = Quote(
            venue=HYPERLIQUID,
            symbol=wanted[name],
            native_symbol=name,
            mark_price=None if mark is None else float(mark),
            index_price=None if ctx.get("oraclePx") is None else float(ctx["oraclePx"]),
            funding=None if funding in (None, "") else Funding(float(funding), 1.0, None),
            volume_24h_usd=None if vol is None else float(vol),
            source_ts_ms=fetch_ts_ms,
            fetch_ts_ms=fetch_ts_ms,
        )
    return out


def parse_book(data: dict, fetch_ts_ms: int) -> Book:
    bids_raw, asks_raw = data["levels"]
    bids = tuple((float(x["px"]), float(x["sz"])) for x in bids_raw)
    asks = tuple((float(x["px"]), float(x["sz"])) for x in asks_raw)
    return Book(bids, asks, int(data.get("time") or fetch_ts_ms), fetch_ts_ms)


class HyperliquidAdapter:
    venue = HYPERLIQUID

    def __init__(self, client: UpstreamClient, markets: list[MarketMap], base: str = HL_BASE):
        self.client = client
        self.base = base
        self.markets = [m for m in markets if m.hl_coin]

    async def fetch_quotes(self) -> dict[str, Quote]:
        """One metaAndAssetCtxs call per dex in use (main plus any HIP-3 dex)."""
        by_dex: dict[str | None, dict[str, str]] = {}
        for m in self.markets:
            assert m.hl_coin
            by_dex.setdefault(m.hl_dex, {})[m.hl_coin] = m.strike
        out: dict[str, Quote] = {}
        for dex, wanted in by_dex.items():
            body: dict = {"type": "metaAndAssetCtxs"}
            if dex:
                body["dex"] = dex
            data = await self.client.post(f"{self.base}/info", json=body, weight=W_META)
            out.update(parse_quotes(data, wanted, int(time.time() * 1000)))
        return out

    async def fetch_book(self, coin: str) -> Book:
        data = await self.client.post(
            f"{self.base}/info", json={"type": "l2Book", "coin": coin}, weight=W_L2
        )
        return parse_book(data, int(time.time() * 1000))
