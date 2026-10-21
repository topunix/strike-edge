"""Unit tests for app.calc against hand-worked examples."""

import pytest

from app import calc
from app.config import MarketMap, Settings, is_tradfi
from app.models import Funding

NOW = 1_800_000_000_000
# Deep single-level book around mid 100: buy at 100.05, sell at 99.95 => 5 bps each way.
DEEP_ASKS = ((100.05, 10_000.0),)
DEEP_BIDS = ((99.95, 10_000.0),)


def deep_fills(notional: float = 10_000.0) -> calc.FillEstimate:
    return calc.estimate_fills(DEEP_BIDS, DEEP_ASKS, notional)


# --- funding normalization and sign ---------------------------------------
def test_hourly_normalization_1h_vs_8h():
    assert calc.hourly_rate(0.0001, 1) == pytest.approx(0.0001)
    assert calc.hourly_rate(0.0008, 8) == pytest.approx(0.0001)
    assert calc.hourly_rate(0.0004, 4) == pytest.approx(0.0001)


def test_funding_dataclass_hourly():
    assert Funding(0.0008, 8).rate_per_hour == pytest.approx(0.0001)


def test_missing_funding_is_none_not_zero():
    assert calc.hourly_rate(None, 8) is None
    assert calc.funding_pnl_usd("long", None, 10_000, 24) is None
    assert calc.funding_pnl_usd("long", 0.0, 10_000, 24) == 0.0
    assert calc.annualized_apr(None) is None
    assert calc.annualized_apr(0.0) == 0.0


def test_funding_sign_convention():
    # positive rate: longs pay, shorts receive. 10000 * 0.0001 * 24 = 24
    assert calc.funding_pnl_usd("long", 0.0001, 10_000, 24) == pytest.approx(-24.0)
    assert calc.funding_pnl_usd("short", 0.0001, 10_000, 24) == pytest.approx(24.0)
    # negative rate flips it
    assert calc.funding_pnl_usd("long", -0.0001, 10_000, 24) == pytest.approx(24.0)
    assert calc.funding_pnl_usd("short", -0.0001, 10_000, 24) == pytest.approx(-24.0)


def test_apr():
    assert calc.annualized_apr(0.0001) == pytest.approx(0.0001 * 8760)


def test_invalid_side_and_interval():
    with pytest.raises(ValueError):
        calc.funding_pnl_usd("flat", 0.1, 1, 1)
    with pytest.raises(ValueError):
        calc.hourly_rate(0.1, 0)


# --- fees -----------------------------------------------------------------
def test_fee_and_rebate_math():
    assert calc.fee_usd(10_000, 0.0005) == pytest.approx(5.0)
    assert calc.fee_usd(10_000, 0.00045) == pytest.approx(4.5)
    assert calc.fee_usd(10_000, -0.00005) == pytest.approx(-0.5)  # rebate


def test_hip3_fee_multiplier_and_trade_url():
    s = Settings(hl_hip3_fee_multiplier=2.0)
    xyz = MarketMap("TSLA-USD", hl_coin="xyz:TSLA")
    main = MarketMap("BTC-USD", hl_coin="BTC")
    assert s.fee_rate("hyperliquid", "taker", xyz) == pytest.approx(0.0009)
    assert s.fee_rate("hyperliquid", "taker", main) == pytest.approx(0.00045)
    assert s.fee_rate("strike", "maker") == pytest.approx(-0.00005)
    assert s.trade_url() == "https://v2.strikefinance.org"
    assert Settings(strike_referral_code="ABC").trade_url().endswith("accept?code=ABC")


# --- order book walk ------------------------------------------------------
def test_walk_book_single_level():
    assert calc.walk_book(((100.0, 1000.0),), 10_000) == pytest.approx(100.0)


def test_walk_book_two_levels_hand_worked():
    # $1000 through [(100 x 6 = $600), (200 x 5 = $1000)]: 600 at 100 -> 6 base,
    # remaining 400 at 200 -> 2 base. total base 8, avg = 1000 / 8 = 125.
    assert calc.walk_book(((100.0, 6.0), (200.0, 5.0)), 1000) == pytest.approx(125.0)


def test_walk_book_thin_returns_none():
    assert calc.walk_book(((100.0, 1.0),), 10_000) is None
    assert calc.walk_book((), 100) is None


def test_slippage_vs_mid():
    f = deep_fills()
    assert f.buy_avg == pytest.approx(100.05)
    assert f.sell_avg == pytest.approx(99.95)
    assert f.buy_slippage_frac == pytest.approx(0.0005)
    assert f.sell_slippage_frac == pytest.approx(0.0005)
    assert f.sufficient


def test_thin_book_marks_insufficient():
    f = calc.estimate_fills(((99.0, 1.0),), ((101.0, 1.0),), 10_000)
    assert not f.sufficient
    hc = calc.holding_cost("long", 10_000, 24, 0.0005, f, 0.0)
    assert not hc.liquidity_ok and hc.total_cost_usd is None


def test_okx_contract_size_conversion():
    # BTC-USDT-SWAP ctVal 0.01: 250 contracts = 2.5 BTC
    assert calc.okx_contracts_to_base(250, 0.01) == pytest.approx(2.5)
    assert calc.okx_contracts_to_base(3, 0.1, 10) == pytest.approx(3.0)


# --- hand-worked scenario 1: Strike long, taker ---------------------------
def test_scenario_1_strike_long_taker_exact():
    # fees 2 * 10000 * 0.0005 = 10.00; slippage 5 bps * 10000 = 5 per fill => 10.00;
    # funding rate 0.00001/h * 10000 * 24h = 2.40 paid. total = 22.40
    hc = calc.holding_cost("long", 10_000, 24, 0.0005, deep_fills(), 0.00001)
    assert hc.entry_fee_usd + hc.exit_fee_usd == pytest.approx(10.0)
    assert hc.entry_slippage_usd + hc.exit_slippage_usd == pytest.approx(10.0)
    assert hc.funding_cost_usd == pytest.approx(2.40)
    assert hc.total_cost_usd == pytest.approx(22.40)


# --- hand-worked scenario 2: Strike long, maker rebate --------------------
def test_scenario_2_strike_long_maker_rebate_exact():
    # rebate 2 * 10000 * -0.00005 = -1.00; slippage assumed 0; funding paid 2.40.
    # total = -1.00 + 2.40 = 1.40 (cost)
    hc = calc.holding_cost("long", 10_000, 24, -0.00005, None, 0.00001, maker=True)
    assert hc.total_cost_usd == pytest.approx(1.40)


def test_short_receives_positive_funding():
    hc = calc.holding_cost("short", 10_000, 24, 0.0005, deep_fills(), 0.00001)
    assert hc.funding_cost_usd == pytest.approx(-2.40)
    assert hc.total_cost_usd == pytest.approx(10.0 + 10.0 - 2.40)


# --- hand-worked scenario 3: two-leg, long Strike / short Hyperliquid -----
def test_scenario_3_two_leg_exact():
    # long Strike pays 0.00001/h, short HL receives 0.0001/h.
    # carry/h = 10000 * (0.0001 - 0.00001) = 0.90 ; 24h funding = 21.60
    # fees: Strike 2*5.00 = 10.00 + HL 2*4.50 = 9.00 => 19.00
    # slippage: 4 fills * 5.00 = 20.00
    # net = 21.60 - 19.00 - 20.00 = -17.40 ; break-even = 39 / 0.90 = 43.3333 h
    t = calc.two_leg(
        10_000, 24, "strike", "hyperliquid", 0.00001, 0.0001, 0.0005, 0.00045,
        deep_fills(), deep_fills(), long_mark=100.0, short_mark=100.1,
    )  # fmt: skip
    assert t.carry_per_hour_usd == pytest.approx(0.90)
    assert t.funding_net_usd == pytest.approx(21.60)
    assert t.fees_usd == pytest.approx(19.0)
    assert t.slippage_usd == pytest.approx(20.0)
    assert t.net_usd == pytest.approx(-17.40)
    assert t.break_even_hours == pytest.approx(39.0 / 0.9)
    # (100.0 - 100.1) / 100.05 * 10000
    assert t.basis_bps == pytest.approx(-9.995, abs=1e-3)


def test_scenario_4_two_leg_profitable_after_a_week():
    # same costs (39.00), carry 0.90/h * 168h = 151.20 => net 112.20
    t = calc.two_leg(
        10_000, 168, "strike", "hyperliquid", 0.00001, 0.0001, 0.0005, 0.00045,
        deep_fills(), deep_fills(),
    )  # fmt: skip
    assert t.net_usd == pytest.approx(112.20)


def test_two_leg_negative_carry_has_no_break_even():
    t = calc.two_leg(
        10_000, 24, "strike", "okx", 0.0001, 0.00001, 0.0005, 0.0005, deep_fills(), deep_fills()
    )
    assert t.carry_per_hour_usd == pytest.approx(-0.9)
    assert t.break_even_hours is None


def test_two_leg_okx_8h_normalized():
    # OKX short leg 0.0008 per 8h => 0.0001/h; Strike long 0 => carry 1.00/h
    okx_hr = calc.hourly_rate(0.0008, 8)
    t = calc.two_leg(10_000, 10, "strike", "okx", 0.0, okx_hr, 0, 0, deep_fills(), deep_fills())
    assert t.carry_per_hour_usd == pytest.approx(1.0)
    assert t.funding_net_usd == pytest.approx(10.0)


def test_two_leg_missing_funding_is_none():
    t = calc.two_leg(
        10_000, 24, "strike", "okx", 0.0, None, 0.0005, 0.0005, deep_fills(), deep_fills()
    )
    assert t.net_usd is None and t.carry_per_hour_usd is None


def test_break_even_helper():
    assert calc.break_even_hours(39.0, 0.9) == pytest.approx(43.3333333)
    assert calc.break_even_hours(39.0, 0.0) is None
    assert calc.break_even_hours(39.0, -1.0) is None
    assert calc.break_even_hours(-5.0, 1.0) == 0.0  # net rebate: already covered


# --- period presets -------------------------------------------------------
def test_period_presets_math():
    from app.config import PRESETS_HOURS

    # carry 0.9/h, round trip 39: 1d 21.6-39=-17.4 ; 1w 151.2-39=112.2 ; 1m 648-39=609
    got = calc.preset_nets(0.9, 39.0, PRESETS_HOURS)
    assert got["1d"] == pytest.approx(-17.4)
    assert got["1w"] == pytest.approx(112.2)
    assert got["1m"] == pytest.approx(609.0)
    assert calc.preset_nets(None, 39.0, PRESETS_HOURS) == {"1d": None, "1w": None, "1m": None}


# --- staleness, confidence, ranking ---------------------------------------
def test_stale_exclusion():
    assert calc.is_stale(NOW - 6 * 60_000, NOW)
    assert not calc.is_stale(NOW - 4 * 60_000, NOW)
    assert calc.is_stale(None, NOW)
    opps = [
        calc.Opportunity("A", "strike", "okx", 50.0),
        calc.Opportunity("B", "strike", "okx", 900.0, stale=True),
        calc.Opportunity("C", "strike", "okx", 70.0, liquidity_ok=False),
        calc.Opportunity("D", "strike", "okx", None),
        calc.Opportunity("E", "strike", "okx", 80.0),
    ]
    assert [o.symbol for o in calc.rank_opportunities(opps)] == ["E", "A"]


def test_rank_by_net_not_raw_spread():
    big_spread_high_cost = calc.Opportunity("X", "strike", "okx", -10.0)
    small_spread_cheap = calc.Opportunity("Y", "strike", "okx", 5.0)
    ranked = calc.rank_opportunities([big_spread_high_cost, small_spread_cheap])
    assert [o.symbol for o in ranked] == ["Y", "X"]


def test_confidence_levels():
    fresh = [NOW - 30_000, NOW - 10_000]
    assert calc.confidence(fresh, NOW, True, 0.95) == "High"
    assert calc.confidence(fresh, NOW, True, 0.6) == "Medium"
    assert calc.confidence([NOW - 3 * 60_000, NOW], NOW, True, 0.95) == "Medium"
    assert calc.confidence(fresh, NOW, False, 0.95) == "Low"
    assert calc.confidence(fresh, NOW, True, 0.3) == "Low"
    assert calc.confidence(fresh, NOW, True, None) == "Low"
    assert calc.confidence([NOW - 10 * 60_000], NOW, True, 0.95) == "Low"


def test_sign_consistency():
    assert calc.sign_consistency([1.0, 2.0, -1.0, None], 1) == pytest.approx(2 / 3)
    assert calc.sign_consistency([None], 1) is None
    assert calc.sign_consistency([1.0], 0) is None


# --- direction toggle -----------------------------------------------------
def test_direction_best_worst_selection():
    best, worst, gap = calc.best_worst_for_direction(
        {"strike": 22.4, "hyperliquid": 30.0, "okx": 18.0, "other": None}
    )
    assert best == ("okx", 18.0)
    assert worst == ("hyperliquid", 30.0)
    assert gap == pytest.approx(12.0)


def test_direction_single_or_no_venue():
    best, worst, gap = calc.best_worst_for_direction({"strike": 5.0, "okx": None})
    assert best == worst == ("strike", 5.0) and gap is None
    assert calc.best_worst_for_direction({"strike": None}) == (None, None, None)


# --- TradFi classification ------------------------------------------------
def test_tradfi_default_rule():
    assert is_tradfi("TSLA-USD", 0.0, 0.0005)
    assert not is_tradfi("BTC-USD", 0.0001, 0.0)
    assert not is_tradfi("X-USD", 0.0, 0.0)
    assert not is_tradfi("X-USD", None, None)


def test_tradfi_override_path():
    assert is_tradfi("BTC-USD", 0.0001, 0.0, force_tradfi={"BTC-USD"}, force_crypto=set())
    assert not is_tradfi("TSLA-USD", 0.0, 0.0005, force_tradfi=set(), force_crypto={"TSLA-USD"})


# --- confidence adjustments (hours, HIP-3 fee, ticker-only) ----------------
FRESH = [NOW - 20_000, NOW - 5_000]


def test_assess_confidence_baseline_high():
    level, reasons = calc.assess_confidence(FRESH, NOW, True, 0.95)
    assert level == "High" and reasons == []


def test_assess_confidence_hip3_fee_lowers_one_level():
    level, reasons = calc.assess_confidence(FRESH, NOW, True, 0.95, fee_unverified=True)
    assert level == "Medium"
    assert any("HL fee unverified (HIP-3 deployer fee not exposed)" in r for r in reasons)
    level, _ = calc.assess_confidence(FRESH, NOW, True, 0.6, fee_unverified=True)
    assert level == "Low"  # Medium lowered to Low


def test_assess_confidence_market_closed_forces_low_with_reason():
    level, reasons = calc.assess_confidence(
        FRESH, NOW, True, 0.95, market_open=False, closed_reason="US stock market holiday"
    )
    assert level == "Low"
    assert any("US stock market holiday" in r for r in reasons)


def test_assess_confidence_ticker_only_always_low():
    level, reasons = calc.assess_confidence(FRESH, NOW, True, 0.99, ticker_only=True)
    assert level == "Low" and any("Ticker match only" in r for r in reasons)


def test_assess_confidence_explains_thin_book_and_stale():
    level, reasons = calc.assess_confidence([NOW - 10 * 60_000], NOW, False, None)
    assert level == "Low" and len(reasons) == 3


def test_rank_excludes_ticker_only_unless_flag():
    opps = [
        calc.Opportunity("DRAM", "strike", "okx", 500.0, ticker_only=True),
        calc.Opportunity("BTC", "strike", "okx", 10.0),
    ]
    assert [o.symbol for o in calc.rank_opportunities(opps)] == ["BTC"]
    assert [o.symbol for o in calc.rank_opportunities(opps, include_unverified=True)] == [
        "DRAM",
        "BTC",
    ]
