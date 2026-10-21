"""Underlying market hours by asset class. Pure functions, no external calendar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import FUTURES_HOLIDAYS, US_EQUITY_HOLIDAYS

NY = ZoneInfo("America/New_York")
SEOUL = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class MarketStatus:
    open: bool
    reason: str | None = None  # why it is closed


def market_status(asset_class: str, now_ms: int) -> MarketStatus:
    """Is the underlying market open at `now_ms` (UTC epoch ms)?"""
    utc = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    if asset_class in ("crypto", "private"):
        return MarketStatus(True)
    if asset_class == "us_equity":
        t = utc.astimezone(NY)
        if t.date().isoformat() in US_EQUITY_HOLIDAYS:
            return MarketStatus(False, "US stock market holiday")
        if t.weekday() >= 5:
            return MarketStatus(False, "US stock market closed for the weekend")
        minutes = t.hour * 60 + t.minute
        if not (9 * 60 + 30 <= minutes < 16 * 60):
            return MarketStatus(False, "Outside US regular trading hours (09:30-16:00 ET)")
        return MarketStatus(True)
    if asset_class == "kr_equity":
        t = utc.astimezone(SEOUL)
        if t.weekday() >= 5:
            return MarketStatus(False, "Korean stock market closed for the weekend")
        minutes = t.hour * 60 + t.minute
        if not (9 * 60 <= minutes < 15 * 60 + 30):
            return MarketStatus(False, "Outside Korean regular trading hours (09:00-15:30 KST)")
        return MarketStatus(True)
    if asset_class == "futures_24x5":
        t = utc.astimezone(NY)
        if t.date().isoformat() in FUTURES_HOLIDAYS:
            return MarketStatus(False, "Futures market holiday")
        wd, minutes = t.weekday(), t.hour * 60 + t.minute
        if wd == 5 or (wd == 4 and minutes >= 17 * 60) or (wd == 6 and minutes < 18 * 60):
            return MarketStatus(False, "Futures-referenced market closed for the weekend")
        if wd in (0, 1, 2, 3) and 17 * 60 <= minutes < 18 * 60:
            return MarketStatus(False, "Futures daily maintenance break (17:00-18:00 ET)")
        return MarketStatus(True)
    return MarketStatus(True)
