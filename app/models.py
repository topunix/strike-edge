"""Normalized market data shared by adapters, calc and the API layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

Level = tuple[float, float]  # (price, quantity in base units)


@dataclass(frozen=True)
class Funding:
    """Funding rate as published, plus the interval it applies to."""

    rate_per_interval: float
    interval_hours: float
    next_funding_ms: int | None = None

    @property
    def rate_per_hour(self) -> float:
        return self.rate_per_interval / self.interval_hours


@dataclass(frozen=True)
class Book:
    """Order book in base units. bids descending, asks ascending."""

    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    source_ts_ms: int
    fetch_ts_ms: int

    @property
    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2


@dataclass(frozen=True)
class Quote:
    """Live per-market quote from one venue. None means the field is missing."""

    venue: str
    symbol: str  # Strike symbol, e.g. BTC-USD
    native_symbol: str
    mark_price: float | None
    index_price: float | None
    funding: Funding | None
    volume_24h_usd: float | None
    source_ts_ms: int
    fetch_ts_ms: int


class FundingPoint(NamedTuple):
    """One settled funding payment. ts_ms is the settlement time (UTC ms).

    The rate covers the `interval_hours` that ended at ts_ms.
    """

    ts_ms: int
    rate_per_interval: float
    interval_hours: float
