from datetime import UTC, datetime

from app.config import SYMBOL_MAP
from app.hours import market_status


def ms(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


def test_crypto_always_open():
    assert market_status("crypto", ms(2026, 10, 24, 3)).open  # Saturday


def test_us_equity_open_midday_et():
    # Wed 2026-10-21 15:00 UTC = 11:00 EDT
    assert market_status("us_equity", ms(2026, 10, 21, 15)).open


def test_us_equity_closed_before_open_and_after_close():
    assert not market_status("us_equity", ms(2026, 10, 21, 13, 0)).open  # 09:00 EDT
    assert not market_status("us_equity", ms(2026, 10, 21, 20, 30)).open  # 16:30 EDT
    assert market_status("us_equity", ms(2026, 10, 21, 13, 30)).open  # 09:30 EDT


def test_us_equity_winter_offset_uses_est():
    # 2026-12-02 14:45 UTC = 09:45 EST (open); 13:45 UTC = 08:45 EST (closed)
    assert market_status("us_equity", ms(2026, 12, 2, 14, 45)).open
    assert not market_status("us_equity", ms(2026, 12, 2, 13, 45)).open


def test_us_equity_weekend_and_holiday():
    s = market_status("us_equity", ms(2026, 10, 24, 15))
    assert not s.open and "weekend" in s.reason
    h = market_status("us_equity", ms(2026, 11, 26, 15))  # Thanksgiving
    assert not h.open and "holiday" in h.reason


def test_kr_equity_hours():
    assert market_status("kr_equity", ms(2026, 10, 21, 2)).open  # 11:00 KST
    assert not market_status("kr_equity", ms(2026, 10, 21, 8)).open  # 17:00 KST
    assert not market_status("kr_equity", ms(2026, 10, 24, 2)).open  # Saturday


def test_futures_24x5_weekend_and_break():
    assert market_status("futures_24x5", ms(2026, 10, 21, 3)).open  # Wed 23:00 ET prior day
    assert not market_status("futures_24x5", ms(2026, 10, 24, 15)).open  # Saturday
    assert not market_status("futures_24x5", ms(2026, 10, 23, 21 + 0, 30)).open  # Fri 17:30 ET
    assert not market_status("futures_24x5", ms(2026, 10, 25, 20)).open  # Sun 16:00 ET
    assert market_status("futures_24x5", ms(2026, 10, 25, 23)).open  # Sun 19:00 ET
    assert not market_status("futures_24x5", ms(2026, 10, 21, 21, 30)).open  # Wed 17:30 ET break


def test_every_market_has_a_class_and_private_names_are_ticker_only():
    assert {m.asset_class for m in SYMBOL_MAP.values()} == {
        "crypto", "us_equity", "kr_equity", "futures_24x5", "private"
    }  # fmt: skip
    only = sorted(m.base for m in SYMBOL_MAP.values() if m.ticker_only)
    assert only == ["CXMT", "DRAM", "MINIMAX", "SPCX", "UNITREE", "ZHIPU"]


def test_hip3_fee_unverified_flag():
    assert SYMBOL_MAP["TSLA-USD"].fee_unverified("hyperliquid")
    assert not SYMBOL_MAP["BTC-USD"].fee_unverified("hyperliquid")
    assert not SYMBOL_MAP["TSLA-USD"].fee_unverified("okx")
