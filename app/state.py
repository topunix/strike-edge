"""In-memory live state written by the poller and read by the engine and API."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import ALL_VENUES
from app.models import Book, Quote


@dataclass
class LiveState:
    quotes: dict[str, dict[str, Quote]] = field(default_factory=lambda: {v: {} for v in ALL_VENUES})
    books: dict[tuple[str, str], Book] = field(default_factory=dict)
    tradfi: dict[str, bool] = field(default_factory=dict)  # Strike symbol -> TradFi/RWA
    last_ok_ms: dict[str, int] = field(default_factory=dict)
    last_error: dict[str, str] = field(default_factory=dict)
    last_error_ms: dict[str, int] = field(default_factory=dict)
    cycles: int = 0
    last_cycle_ms: int | None = None

    def set_quotes(self, venue: str, quotes: dict[str, Quote], now_ms: int) -> None:
        self.quotes[venue].update(quotes)
        self.last_ok_ms[venue] = now_ms

    def set_book(self, venue: str, symbol: str, book: Book) -> None:
        self.books[(venue, symbol)] = book

    def record_error(self, venue: str, message: str, now_ms: int) -> None:
        self.last_error[venue] = message
        self.last_error_ms[venue] = now_ms

    def newest_source_ts(self, venue: str) -> int | None:
        ts = [q.source_ts_ms for q in self.quotes.get(venue, {}).values()]
        return max(ts) if ts else None
