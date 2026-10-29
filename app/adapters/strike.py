"""Strike adapter. Public endpoints, no auth.

Request weights are not published per endpoint; we assume conventional weights
(premiumIndex 10, ticker/24hr without symbol 40, exchangeInfo 10, depth limit 50: 5)
which is conservative for our 50% budget.
"""

from __future__ import annotations

import time
from typing import Any

from app.adapters.base import TokenBucket, UpstreamClient
from app.config import SLP_VAULT_ID, STRIKE, STRIKE_BASE, MarketMap, is_tradfi
from app.models import Book, Funding, FundingPoint, LiquiditySample, Quote

W_PREMIUM, W_TICKER, W_INFO, W_DEPTH, W_HISTORY = 10, 40, 10, 5, 10
W_CREDIT, W_VAULTS, W_STATS = 5, 5, 10  # assumed, like the weights above
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


def parse_credit_debt(data: dict) -> dict[str, float] | None:
    """v2/credit/collateral-assets -> total_debt_usd per collateral asset.

    None unless every asset reports a debt figure: a total built from some assets would
    understate borrowed capital.
    """
    out: dict[str, float] = {}
    for asset, row in (data.get("assets") or {}).items():
        debt = _f(row.get("total_debt_usd"))
        if debt is None:
            return None
        out[asset] = debt
    return out or None


def parse_slp_vault(data: dict, vault_id: str = SLP_VAULT_ID) -> tuple[float | None, float | None]:
    """v2/vaults?type=protocol -> (SLP equity in USD, share price). TVL is the vault equity."""
    for v in data.get("vaults", []):
        if v.get("vault_id") == vault_id:
            return _f(v.get("equity")), _f(v.get("share_price"))
    return None, None


def parse_slp_history(data: dict) -> list[tuple[int, float, float | None]]:
    """vault portfolio history -> (ts_ms, tvl_usd, share_price) rows, oldest first.

    Row layout: [ts_ms, tvl, pnl, fees_earned, depositors, share_price]. Rows without a
    numeric TVL are skipped; a missing share price stays None.
    """
    out = []
    for row in data.get("history") or []:
        tvl = _f(row[1]) if len(row) > 1 else None
        if tvl is not None:
            out.append((int(row[0]), tvl, _f(row[5]) if len(row) > 5 else None))
    return sorted(out)


def parse_daily_volume(data: dict) -> dict[int, float]:
    """stat/v1/dashboard/volumes -> platform USD volume per UTC day start (ms)."""
    out: dict[int, float] = {}
    for day_ms, _symbol, usd in data.get("total_volume_daily") or []:
        v = _f(usd)
        if v is not None:
            out[int(day_ms)] = out.get(int(day_ms), 0.0) + v
    return out


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
            low_priority=True,
        )
        return parse_funding_history(data, start_ms, end_ms)

    async def fetch_liquidity(self) -> LiquiditySample:
        """Debt per collateral asset and SLP TVL. Each half fails independently (None).

        Raises only when both are unavailable, so the caller records one error.
        """
        now = int(time.time() * 1000)
        debt: dict[str, float] | None = None
        tvl = share_price = None
        errors: list[Exception] = []
        try:
            data = await self.client.get(
                f"{self.base}/v2/credit/collateral-assets", weight=W_CREDIT
            )
            debt = parse_credit_debt(data)
        except Exception as exc:
            errors.append(exc)
        try:
            data = await self.client.get(
                f"{self.base}/v2/vaults", params={"type": "protocol"}, weight=W_VAULTS
            )
            tvl, share_price = parse_slp_vault(data)
        except Exception as exc:
            errors.append(exc)
        if len(errors) == 2:
            raise errors[0]
        total = None if debt is None else sum(debt.values())
        return LiquiditySample(now, tvl, share_price, total, debt)

    async def fetch_slp_history(self, period: str) -> list[tuple[int, float, float | None]]:
        """SLP TVL and share price history: hourly for period 30d, daily for 6m."""
        data = await self.client.get(
            f"{self.base}/v2/vault/{SLP_VAULT_ID}/portfolio",
            params={"period": period},
            weight=W_STATS,
            low_priority=True,
        )
        return parse_slp_history(data)

    async def fetch_daily_volume(self) -> dict[int, float]:
        """Platform volume per UTC day (single-sided). The current day is partial."""
        data = await self.client.get(
            f"{self.base}/stat/v1/dashboard/volumes", weight=W_STATS, low_priority=True
        )
        return parse_daily_volume(data)
