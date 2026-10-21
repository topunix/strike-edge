"""Strike adapter. Public endpoints, no auth.

Request weights are not published per endpoint; we assume conventional weights
(premiumIndex 10, ticker/24hr without symbol 40, exchangeInfo 10, depth limit 50: 5)
which is conservative for our 50% budget.
"""

from __future__ import annotations

import time
from typing import Any

from app.adapters.base import TokenBucket, UpstreamClient
from app.config import STRIKE, STRIKE_BASE, MarketMap, is_tradfi
from app.models import Book, Funding, FundingPoint, Quote

W_PREMIUM, W_TICKER, W_INFO, W_DEPTH, W_HISTORY = 10, 40, 10, 5, 10
DEFAULT_WEIGHT_PER_MIN = 2400


def _f(x: Any) -> float | None:
    if x is None or x == "":
        return None
    return float(x)


def parse_rate_limit_weight(info: dict) -> int | None:
    """REQUEST_WEIGHT per minute from /v2/exchangeInfo rateLimits."""
    for rl in info.get("rateLimits", []):
        if rl.get("rateLimitType") == "REQUEST_WEIGHT" and rl.get("interval") == "MINUTE":
            return int(rl["limit"]) // int(rl.get("intervalNum", 1))
    return None


def parse_quotes(
    premium: list[dict], tickers: list[dict], fetch_ts_ms: int
) -> tuple[dict[str, Quote], dict[str, bool]]:
    """premiumIndex + ticker/24hr -> quotes by Strike symbol, plus TradFi flags."""
    vol = {t["symbol"]: _f(t.get("quoteVolume")) for t in tickers}
    quotes: dict[str, Quote] = {}
    tradfi: dict[str, bool] = {}
    for p in premium:
        sym = p["symbol"]
        rate = _f(p.get("fundingRate"))
        quotes[sym] = Quote(
            venue=STRIKE,
            symbol=sym,
            native_symbol=sym,
            mark_price=_f(p.get("markPrice")),
            index_price=_f(p.get("indexPrice")),
            funding=None if rate is None else Funding(rate, 1.0, p.get("nextFundingTime")),
            volume_24h_usd=vol.get(sym),
            source_ts_ms=int(p["time"]),
            fetch_ts_ms=fetch_ts_ms,
        )
        tradfi[sym] = is_tradfi(sym, _f(p.get("interestRate")), _f(p.get("interestRateDampener")))
    return quotes, tradfi


def parse_book(data: dict, fetch_ts_ms: int) -> Book:
    bids = tuple((float(p), float(q)) for p, q in data.get("bids", []))
    asks = tuple((float(p), float(q)) for p, q in data.get("asks", []))
    ts = int(data.get("T") or data.get("E") or fetch_ts_ms)
    return Book(bids, asks, ts, fetch_ts_ms)


def parse_funding_history(data: dict, start_ms: int, end_ms: int) -> list[FundingPoint]:
    """stats/coin/history/funding -> hourly points within [start_ms, end_ms]."""
    out = [
        FundingPoint(int(ts), float(rate), 1.0)
        for ts, rate in data.get("data", [])
        if rate is not None and start_ms <= int(ts) <= end_ms
    ]
    return sorted(out)


class StrikeAdapter:
    venue = STRIKE

    def __init__(self, client: UpstreamClient, base: str = STRIKE_BASE) -> None:
        self.client = client
        self.base = base
        self.tradfi: dict[str, bool] = {}

    @staticmethod
    def make_bucket(weight_per_min: int = DEFAULT_WEIGHT_PER_MIN, fraction: float = 0.5):
        return TokenBucket.per_window(weight_per_min, 60.0, fraction)

    async def load_rate_limits(self, fraction: float = 0.5) -> int | None:
        """Read rateLimits from exchangeInfo at startup and size the bucket from it."""
        info = await self.client.get(f"{self.base}/price/v2/exchangeInfo", weight=W_INFO)
        limit = parse_rate_limit_weight(info)
        if limit:
            self.client.bucket = self.make_bucket(limit, fraction)
        return limit

    async def fetch_quotes(self) -> dict[str, Quote]:
        premium = await self.client.get(f"{self.base}/price/v2/premiumIndex", weight=W_PREMIUM)
        try:
            tickers = await self.client.get(f"{self.base}/price/v2/ticker/24hr", weight=W_TICKER)
        except Exception:  # volume is optional; keep funding and marks
            tickers = []
        quotes, self.tradfi = parse_quotes(premium, tickers, int(time.time() * 1000))
        return quotes

    async def fetch_book(self, symbol: str, limit: int = 50) -> Book:
        data = await self.client.get(
            f"{self.base}/price/v2/depth",
            params={"symbol": symbol, "limit": limit},
            weight=W_DEPTH,
        )
        return parse_book(data, int(time.time() * 1000))

    async def fetch_funding_history(
        self, market: MarketMap, start_ms: int, end_ms: int
    ) -> list[FundingPoint]:
        """Hourly funding history. The API takes whole days (1 to 90) back from now."""
        days = min(90, max(1, -(-(int(time.time() * 1000) - start_ms) // 86_400_000) + 1))
        data = await self.client.get(
            f"{self.base}/stat/v1/stats/coin/history/funding",
            params={"symbol": market.strike, "days": days},
            weight=W_HISTORY,
        )
        return parse_funding_history(data, start_ms, end_ms)
