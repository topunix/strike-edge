"""OKX adapter (USDT-margined linear swaps).

Batch endpoints: funding-rate?instId=ANY, public/mark-price?instType=SWAP and
market/tickers?instType=SWAP cover all instruments in 3 calls per cycle.
Funding interval = nextFundingTime - fundingTime of each instrument (1h, 4h, 8h...).
Order book sizes are contracts; base units = contracts * ctVal * ctMult.
Published limits (docs): funding-rate 10/2s, funding-rate-history 10/2s,
mark-price 10/2s, tickers 20/2s, books 40/2s, instruments 20/2s. We use 50%.
"""

from __future__ import annotations

import time

from app import calc
from app.adapters.base import TokenBucket, UpstreamClient
from app.config import OKX, OKX_BASE, MarketMap
from app.models import Book, Funding, Quote

ENDPOINT_LIMITS = {  # (requests, per seconds)
    "funding-rate": (10, 2),
    "funding-rate-history": (10, 2),
    "mark-price": (10, 2),
    "tickers": (20, 2),
    "books": (40, 2),
    "instruments": (20, 2),
}


def make_buckets(fraction: float = 0.5) -> dict[str, TokenBucket]:
    return {k: TokenBucket.per_window(n, w, fraction) for k, (n, w) in ENDPOINT_LIMITS.items()}


def make_bucket(fraction: float = 0.5) -> TokenBucket:
    return TokenBucket.per_window(40, 2.0, fraction)


def _f(x: str | None) -> float | None:
    return None if x in (None, "") else float(x)


def parse_contract_sizes(instruments: dict) -> dict[str, float]:
    """instId -> base units per contract (ctVal * ctMult). Linear swaps only."""
    out: dict[str, float] = {}
    for i in instruments["data"]:
        if i.get("ctType") != "linear":
            continue
        out[i["instId"]] = float(i["ctVal"]) * float(i.get("ctMult") or 1)
    return out


def parse_quotes(
    funding: dict,
    marks: dict,
    tickers: dict,
    wanted: dict[str, str],
    fetch_ts_ms: int,
) -> dict[str, Quote]:
    """`wanted` maps OKX instId -> Strike symbol."""
    mark_by = {m["instId"]: m for m in marks["data"]}
    tick_by = {t["instId"]: t for t in tickers["data"]}
    out: dict[str, Quote] = {}
    for fr in funding["data"]:
        inst = fr["instId"]
        if inst not in wanted:
            continue
        rate = _f(fr.get("fundingRate"))
        nxt, cur = _f(fr.get("nextFundingTime")), _f(fr.get("fundingTime"))
        f: Funding | None = None
        if rate is not None and nxt is not None and cur is not None and nxt > cur:
            f = Funding(rate, (nxt - cur) / 3_600_000, int(nxt))
        mk = mark_by.get(inst)
        tk = tick_by.get(inst)
        last = _f(tk.get("last")) if tk else None
        base_vol = _f(tk.get("volCcy24h")) if tk else None
        out[wanted[inst]] = Quote(
            venue=OKX,
            symbol=wanted[inst],
            native_symbol=inst,
            mark_price=_f(mk.get("markPx")) if mk else None,
            index_price=None,
            funding=f,
            volume_24h_usd=None if base_vol is None or last is None else base_vol * last,
            source_ts_ms=int(fr.get("ts") or fetch_ts_ms),
            fetch_ts_ms=fetch_ts_ms,
        )
    return out


def parse_book(data: dict, ct_base: float, fetch_ts_ms: int) -> Book:
    d = data["data"][0]
    bids = tuple((float(r[0]), calc.okx_contracts_to_base(float(r[1]), ct_base)) for r in d["bids"])
    asks = tuple((float(r[0]), calc.okx_contracts_to_base(float(r[1]), ct_base)) for r in d["asks"])
    return Book(bids, asks, int(d.get("ts") or fetch_ts_ms), fetch_ts_ms)


class OkxAdapter:
    venue = OKX

    def __init__(self, client: UpstreamClient, markets: list[MarketMap], base: str = OKX_BASE):
        self.client = client
        self.base = base
        self.markets = [m for m in markets if m.okx_inst]
        self.contract_base: dict[str, float] = {}  # instId -> base per contract

    async def load_instruments(self) -> None:
        data = await self.client.get(
            f"{self.base}/api/v5/public/instruments",
            params={"instType": "SWAP"},
            endpoint="instruments",
        )
        self.contract_base = parse_contract_sizes(data)

    async def fetch_quotes(self) -> dict[str, Quote]:
        wanted = {m.okx_inst: m.strike for m in self.markets if m.okx_inst}
        g = self.client.get
        funding = await g(
            f"{self.base}/api/v5/public/funding-rate",
            params={"instId": "ANY"},
            endpoint="funding-rate",
        )
        marks = await g(
            f"{self.base}/api/v5/public/mark-price",
            params={"instType": "SWAP"},
            endpoint="mark-price",
        )
        tickers = await g(
            f"{self.base}/api/v5/market/tickers",
            params={"instType": "SWAP"},
            endpoint="tickers",
        )
        return parse_quotes(funding, marks, tickers, wanted, int(time.time() * 1000))

    async def fetch_book(self, inst_id: str, sz: int = 50) -> Book:
        if not self.contract_base:
            await self.load_instruments()
        ct = self.contract_base.get(inst_id)
        if ct is None:
            raise ValueError(f"unknown or non-linear OKX instrument {inst_id}")
        data = await self.client.get(
            f"{self.base}/api/v5/market/books",
            params={"instId": inst_id, "sz": sz},
            endpoint="books",
        )
        return parse_book(data, ct, int(time.time() * 1000))
