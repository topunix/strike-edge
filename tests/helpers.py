"""Build a realistic LiveState and app from the saved real API fixtures."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app import report
from app.adapters import hyperliquid as hl
from app.adapters import okx, strike
from app.config import HYPERLIQUID, OKX, STRIKE, SYMBOL_MAP
from app.db import Store
from app.models import Book, FundingPoint
from app.state import LiveState

FIX = Path(__file__).parent / "fixtures"
WED_NOON_UTC = int(datetime(2026, 10, 21, 15, 30, tzinfo=UTC).timestamp() * 1000)  # 11:30 EDT
HOUR = 3_600_000


def load(name: str):
    return json.loads((FIX / name).read_text())


def synthetic_book(
    mid: float, now: int, depth_usd: float = 2_000_000, half_spread_bps: float = 1.0
):
    """Three-level book, `half_spread_bps` each side of mid, `depth_usd` per side."""
    h = mid * half_spread_bps / 1e4
    qty = depth_usd / mid / 3
    bids = tuple((mid - h - i * h, qty) for i in range(3))
    asks = tuple((mid + h + i * h, qty) for i in range(3))
    return Book(bids, asks, now - 5_000, now - 5_000)


def build_state(now: int = WED_NOON_UTC, books: bool = True) -> LiveState:
    st = LiveState()
    fetch = now - 10_000
    quotes, tradfi = strike.parse_quotes(
        load("strike_premiumIndex.json"), load("strike_ticker24hr.json"), fetch
    )
    st.tradfi = tradfi
    main = {m.hl_coin: m.strike for m in SYMBOL_MAP.values() if m.hl_coin and not m.hl_dex}
    xyz = {m.hl_coin: m.strike for m in SYMBOL_MAP.values() if m.hl_dex == "xyz"}
    hq = hl.parse_quotes(load("hl_metaAndAssetCtxs.json"), main, fetch)
    hq.update(hl.parse_quotes(load("hl_metaAndAssetCtxs_xyz.json"), xyz, fetch))
    wanted = {m.okx_inst: m.strike for m in SYMBOL_MAP.values() if m.okx_inst}
    oq = okx.parse_quotes(
        load("okx_funding_rate_ANY.json"),
        load("okx_markprice_SWAP.json"),
        load("okx_tickers_SWAP.json"),
        wanted,
        fetch,
    )
    for venue, qs in ((STRIKE, quotes), (HYPERLIQUID, hq), (OKX, oq)):
        # Re-stamp source times so "now" is deterministic and fresh.
        st.quotes[venue] = {s: replace(q, source_ts_ms=now - 10_000) for s, q in qs.items()}
        st.last_ok_ms[venue] = fetch
        if books:
            for s, q in st.quotes[venue].items():
                if q.mark_price:
                    st.books[(venue, s)] = synthetic_book(q.mark_price, now)
    return st


def seed_history(store: Store, now: int, symbols=("BTC-USD", "ETH-USD", "TSLA-USD")) -> None:
    """30 days of simple funding for a few markets: Strike 0.00001/h; others differ."""
    hours = report.window_hours(now, 30)
    for sym in symbols:
        m = SYMBOL_MAP[sym]
        store.upsert_funding(STRIKE, sym, [FundingPoint(h + HOUR, 0.00001, 1.0) for h in hours])
        if m.hl_coin:
            store.upsert_funding(
                HYPERLIQUID, sym, [FundingPoint(h + HOUR, 0.0001, 1.0) for h in hours]
            )
        if m.okx_inst:
            store.upsert_funding(
                OKX, sym, [FundingPoint(h + HOUR, 0.0008, 8.0) for h in hours[7::8]]
            )
