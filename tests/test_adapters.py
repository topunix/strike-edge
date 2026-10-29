import pytest

from app import calc
from app.adapters import hyperliquid as hl
from app.adapters import okx, strike
from app.config import SYMBOL_MAP, is_tradfi

NOW = 1_792_596_600_000


def test_strike_quotes_and_tradfi_default_rule(fx):
    quotes, tradfi = strike.parse_quotes(
        fx("strike_premiumIndex.json"), fx("strike_ticker24hr.json"), NOW
    )
    assert len(quotes) == 31
    btc = quotes["BTC-USD"]
    assert btc.funding is not None and btc.funding.interval_hours == 1.0
    assert btc.funding.rate_per_hour == btc.funding.rate_per_interval
    assert btc.volume_24h_usd and btc.mark_price and btc.source_ts_ms > 0
    assert tradfi["TSLA-USD"] and tradfi["XAU-USD"] and tradfi["WTI-USD"]
    assert not tradfi["BTC-USD"] and not tradfi["NIGHT-USD"]
    assert sum(tradfi.values()) == 20  # 31 markets minus 11 crypto


def test_strike_override_beats_default_rule(fx):
    assert not is_tradfi("TSLA-USD", 0.0, 0.0005, force_tradfi=set(), force_crypto={"TSLA-USD"})
    assert is_tradfi("NIGHT-USD", 0.0001, 0.0, force_tradfi={"NIGHT-USD"}, force_crypto=set())


def test_strike_zero_funding_vs_missing(fx):
    prem = [dict(fx("strike_premiumIndex.json")[0])]
    prem[0]["fundingRate"] = "0"
    q, _ = strike.parse_quotes(prem, [], NOW)
    assert q[prem[0]["symbol"]].funding.rate_per_hour == 0.0
    prem[0]["fundingRate"] = ""
    q, _ = strike.parse_quotes(prem, [], NOW)
    assert q[prem[0]["symbol"]].funding is None
    assert q[prem[0]["symbol"]].volume_24h_usd is None


def test_strike_book(fx):
    b = strike.parse_book(fx("strike_depth_BTC-USD.json"), NOW)
    assert b.bids[0][0] < b.asks[0][0]
    assert b.bids[0][0] > b.bids[-1][0]
    assert b.source_ts_ms > 0


def test_hl_main_and_dex_quotes(fx):
    main = hl.parse_quotes(
        fx("hl_metaAndAssetCtxs.json"), {"BTC": "BTC-USD", "PUMP": "PUMP-USD"}, NOW
    )
    assert main["BTC-USD"].funding.interval_hours == 1.0
    assert main["BTC-USD"].mark_price == pytest.approx(84422.0)
    xyz = hl.parse_quotes(
        fx("hl_metaAndAssetCtxs_xyz.json"), {"xyz:TSLA": "TSLA-USD", "xyz:GOLD": "XAU-USD"}, NOW
    )
    assert set(xyz) == {"TSLA-USD", "XAU-USD"}
    assert xyz["TSLA-USD"].native_symbol == "xyz:TSLA"


def test_hl_books(fx):
    b = hl.parse_book(fx("hl_l2Book_BTC.json"), NOW)
    assert b.bids[0][0] < b.asks[0][0] and len(b.bids) > 5
    x = hl.parse_book(fx("hl_l2Book_xyz_TSLA.json"), NOW)
    assert x.mid and 300 < x.mid < 500


def test_okx_quotes_intervals_and_zero_vs_missing(fx):
    wanted = {"BTC-USDT-SWAP": "BTC-USD", "NIGHT-USDT-SWAP": "NIGHT-USD", "CL-USDT-SWAP": "WTI-USD"}
    q = okx.parse_quotes(
        fx("okx_funding_rate_ANY.json"),
        fx("okx_markprice_SWAP.json"),
        fx("okx_tickers_SWAP.json"),
        wanted,
        NOW,
    )
    assert q["BTC-USD"].funding.interval_hours == 8.0
    assert q["NIGHT-USD"].funding.interval_hours == 4.0
    assert q["WTI-USD"].funding.interval_hours == 4.0
    btc = q["BTC-USD"].funding
    assert btc.rate_per_hour == pytest.approx(btc.rate_per_interval / 8)
    assert q["BTC-USD"].mark_price and q["BTC-USD"].volume_24h_usd

    one = {"data": [dict(fx("okx_funding_rate_BTC.json")["data"][0])]}
    one["data"][0]["fundingRate"] = "0"
    z = okx.parse_quotes(one, {"data": []}, {"data": []}, {"BTC-USDT-SWAP": "BTC-USD"}, NOW)
    assert z["BTC-USD"].funding.rate_per_hour == 0.0
    assert z["BTC-USD"].mark_price is None  # missing stays None
    one["data"][0]["fundingRate"] = ""
    m = okx.parse_quotes(one, {"data": []}, {"data": []}, {"BTC-USDT-SWAP": "BTC-USD"}, NOW)
    assert m["BTC-USD"].funding is None


def test_okx_contract_size_conversion_in_book(fx):
    sizes = okx.parse_contract_sizes(fx("okx_instruments_SWAP_all.json"))
    assert sizes["BTC-USDT-SWAP"] == pytest.approx(0.01)
    assert "BTC-USD-SWAP" not in sizes  # inverse contracts excluded
    raw = fx("okx_books_BTC.json")
    first_ask_contracts = float(raw["data"][0]["asks"][0][1])
    book = okx.parse_book(raw, sizes["BTC-USDT-SWAP"], NOW)
    assert book.asks[0][1] == pytest.approx(first_ask_contracts * 0.01)
    assert calc.okx_contracts_to_base(first_ask_contracts, 0.01) == pytest.approx(book.asks[0][1])


def test_symbol_map_final_counts():
    m = SYMBOL_MAP
    assert len(m) == 31
    assert all(v.okx_inst for v in m.values())  # every Strike market matched on OKX
    hl_main = [v for v in m.values() if v.hl_coin and not v.hl_dex]
    hl_xyz = [v for v in m.values() if v.hl_dex == "xyz"]
    assert len(hl_main) == 10 and len(hl_xyz) == 18
    no_hl = sorted(v.strike for v in m.values() if not v.hl_coin)
    assert no_hl == ["NAS100-USD", "NIGHT-USD", "SKHYNIX-USD"]
    assert m["XAU-USD"].hl_coin == "xyz:GOLD" and m["XAU-USD"].okx_inst == "XAU-USDT-SWAP"
    assert m["WTI-USD"].okx_inst == "CL-USDT-SWAP" and m["WTI-USD"].note


def test_strike_credit_debt_per_asset_and_total(fx):
    debt = strike.parse_credit_debt(fx("strike_credit_collateral_assets.json"))
    assert set(debt) == {
        "ADA",
        "LP_SUNDAE_USDCx_USDM",
        "SCALED_AAVE_V3_ETH_aUSDC",
        "enzoBTC",
        "sUSDrf",
    }
    assert debt["enzoBTC"] == 100.0
    assert sum(debt.values()) == pytest.approx(406_313.99, abs=0.01)


def test_strike_credit_debt_is_none_when_any_asset_is_missing(fx):
    data = fx("strike_credit_collateral_assets.json")
    del data["assets"]["ADA"]["total_debt_usd"]
    assert strike.parse_credit_debt(data) is None  # a partial total would understate borrowing
    assert strike.parse_credit_debt({"assets": {}}) is None
    assert strike.parse_credit_debt({}) is None


def test_strike_slp_vault_tvl_is_equity_and_share_price(fx):
    tvl, share = strike.parse_slp_vault(fx("strike_vaults_protocol.json"))
    assert tvl == pytest.approx(930_974.02, abs=0.01) and share == pytest.approx(1.16615, abs=1e-5)
    assert strike.parse_slp_vault(fx("strike_vaults_protocol.json"), "other-vault") == (None, None)
    assert strike.parse_slp_vault({"vaults": []}) == (None, None)
