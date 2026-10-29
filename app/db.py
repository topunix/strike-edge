"""SQLite storage: funding history cache, backfill state, snapshots and liquidity samples."""

from __future__ import annotations

import json
import sqlite3
import threading
import zlib
from dataclasses import dataclass

from app.models import FundingPoint, LiquiditySample

SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_history (
  venue TEXT NOT NULL, symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
  rate REAL NOT NULL, interval_hours REAL NOT NULL,
  PRIMARY KEY (venue, symbol, ts_ms)
);
CREATE TABLE IF NOT EXISTS snapshots (
  ts_ms INTEGER PRIMARY KEY, payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS liquidity_samples (
  ts_ms INTEGER PRIMARY KEY,
  slp_tvl_usd REAL, slp_share_price REAL,
  debt_total_usd REAL, debt_by_asset TEXT,
  volume_24h_usd REAL,
  published_apr REAL
);
CREATE TABLE IF NOT EXISTS backfill_state (
  venue TEXT NOT NULL, symbol TEXT NOT NULL,
  covered_from_ms INTEGER, covered_to_ms INTEGER, last_checked_ms INTEGER,
  status TEXT NOT NULL, error TEXT,
  PRIMARY KEY (venue, symbol)
);
"""


@dataclass(frozen=True)
class BackfillState:
    venue: str
    symbol: str
    covered_from_ms: int | None
    covered_to_ms: int | None
    last_checked_ms: int | None
    status: str
    error: str | None


class Store:
    """Thread-safe SQLite wrapper. Pass ':memory:' for tests."""

    def __init__(self, path: str = "strikeedge.db") -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(liquidity_samples)")}
            if "published_apr" not in cols:  # table created before the column existed
                self._db.execute("ALTER TABLE liquidity_samples ADD COLUMN published_apr REAL")
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- funding history ---------------------------------------------------
    def upsert_funding(self, venue: str, symbol: str, points: list[FundingPoint]) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO funding_history VALUES (?,?,?,?,?)",
                [(venue, symbol, p.ts_ms, p.rate_per_interval, p.interval_hours) for p in points],
            )

    def get_funding(
        self, venue: str, symbol: str, since_ms: int = 0, until_ms: int | None = None
    ) -> list[FundingPoint]:
        q = (
            "SELECT ts_ms, rate, interval_hours FROM funding_history "
            "WHERE venue=? AND symbol=? AND ts_ms>=?"
        )
        args: list = [venue, symbol, since_ms]
        if until_ms is not None:
            q += " AND ts_ms<=?"
            args.append(until_ms)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY ts_ms", args).fetchall()
        return [FundingPoint(r[0], r[1], r[2]) for r in rows]

    def prune_funding(self, older_than_ms: int) -> int:
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM funding_history WHERE ts_ms<?", (older_than_ms,)
            ).rowcount

    # -- backfill state ----------------------------------------------------
    def get_state(self, venue: str, symbol: str) -> BackfillState | None:
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM backfill_state WHERE venue=? AND symbol=?", (venue, symbol)
            ).fetchone()
        return None if r is None else BackfillState(*tuple(r))

    def set_state(self, st: BackfillState) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO backfill_state VALUES (?,?,?,?,?,?,?)",
                (
                    st.venue,
                    st.symbol,
                    st.covered_from_ms,
                    st.covered_to_ms,
                    st.last_checked_ms,
                    st.status,
                    st.error,
                ),
            )

    def all_states(self) -> list[BackfillState]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM backfill_state").fetchall()
        return [BackfillState(*tuple(r)) for r in rows]

    # -- live snapshots (kept 7 days) -------------------------------------
    def save_snapshot(self, ts_ms: int, payload: dict) -> None:
        blob = zlib.compress(json.dumps(payload, separators=(",", ":")).encode())
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?)", (ts_ms, blob))

    def get_snapshots(self, since_ms: int, until_ms: int | None = None) -> list[dict]:
        q, args = "SELECT ts_ms, payload FROM snapshots WHERE ts_ms>=?", [since_ms]
        if until_ms is not None:
            q += " AND ts_ms<=?"
            args.append(until_ms)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY ts_ms", args).fetchall()
        return [json.loads(zlib.decompress(r[1])) | {"ts_ms": r[0]} for r in rows]

    def count_snapshots(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

    def prune_snapshots(self, older_than_ms: int) -> int:
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM snapshots WHERE ts_ms<?", (older_than_ms,)
            ).rowcount

    # -- liquidity quality samples (debt has no upstream history, so keep our own) ---
    def save_liquidity(self, s: LiquiditySample) -> None:
        by_asset = None if s.debt_by_asset is None else json.dumps(s.debt_by_asset)
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO liquidity_samples VALUES (?,?,?,?,?,?,?)",
                (
                    s.ts_ms,
                    s.slp_tvl_usd,
                    s.slp_share_price,
                    s.debt_total_usd,
                    by_asset,
                    s.volume_24h_usd,
                    s.published_apr,
                ),
            )

    def get_liquidity(
        self, since_ms: int, until_ms: int | None = None, thin_before_ms: int | None = None
    ) -> list[LiquiditySample]:
        """Samples oldest first. With `thin_before_ms`, rows older than it keep only the last
        sample of each UTC hour (the minute samples are not needed for old trends)."""
        q, args = "SELECT * FROM liquidity_samples WHERE ts_ms>=?", [since_ms]
        if until_ms is not None:
            q += " AND ts_ms<=?"
            args.append(until_ms)
        if thin_before_ms is not None:
            q += (
                " AND (ts_ms>=? OR ts_ms IN (SELECT MAX(ts_ms) FROM liquidity_samples"
                " WHERE ts_ms<? GROUP BY ts_ms/3600000))"
            )
            args += [thin_before_ms, thin_before_ms]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY ts_ms", args).fetchall()
        return [
            LiquiditySample(r[0], r[1], r[2], r[3], json.loads(r[4]) if r[4] else None, r[5], r[6])
            for r in rows
        ]

    def fill_liquidity(self, samples: list[LiquiditySample]) -> None:
        """Insert backfilled rows without touching live values: only NULL columns are filled."""
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO liquidity_samples"
                " (ts_ms, slp_tvl_usd, slp_share_price, volume_24h_usd) VALUES (?,?,?,?)"
                " ON CONFLICT(ts_ms) DO UPDATE SET"
                " slp_tvl_usd=COALESCE(slp_tvl_usd, excluded.slp_tvl_usd),"
                " slp_share_price=COALESCE(slp_share_price, excluded.slp_share_price),"
                " volume_24h_usd=COALESCE(volume_24h_usd, excluded.volume_24h_usd)",
                [(s.ts_ms, s.slp_tvl_usd, s.slp_share_price, s.volume_24h_usd) for s in samples],
            )

    def prune_liquidity(self, older_than_ms: int) -> int:
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM liquidity_samples WHERE ts_ms<?", (older_than_ms,)
            ).rowcount
