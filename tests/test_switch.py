"""Move to Strike? (switch calculator): hand-worked dollars, API, tab, and edge cases."""

import re
from dataclasses import replace
from pathlib import Path

import pytest

from app import calc
from tests.helpers import WED_NOON_UTC, build_state
from tests.test_web import make

ROOT = Path(__file__).resolve().parent.parent
N, H = 10_000.0, 336.0  # $10,000 for 2 weeks


def fills(buy, sell):
    """A FillEstimate with the given slippage fractions (prices are irrelevant to the maths)."""
    return calc.FillEstimate(100.0, 100.0, buy, sell)


# --- hand-worked scenarios ---------------------------------------------------------------------
def test_long_switch_saves_money_and_pays_back_quickly():
    # Hyperliquid +0.0100%/h (long pays), Strike -0.0050%/h (long is paid).
    s = calc.switch_cost(
        "long", N, H, 0.00045, fills(0.0001, 0.0001), 0.0001,
        0.0005, fills(0.0002, 0.0002), -0.00005,
    )  # fmt: skip
    assert s.stay_funding_usd == pytest.approx(336.0)  # 0.0001 * 10,000 * 336 paid
    assert s.stay_total_cost_usd == pytest.approx(336 + 4.5 + 1.0)  # + exit fee + exit slip
    assert s.strike_funding_usd == pytest.approx(-168.0)  # received
    # close now 4.5 + 1.0, open 5 + 2, funding -168, close at the end 5 + 2
    assert s.switch_total_cost_usd == pytest.approx(5.5 + 7 - 168 + 7)
    assert s.saving_usd == pytest.approx(490.0)
    assert s.upfront_cost_usd == pytest.approx(12.5)  # close now + open on Strike
    assert s.funding_advantage_per_hour_usd == pytest.approx(1.5)
    assert s.break_even_hours == pytest.approx(14 / 1.5)  # Strike entry + exit / advantage
    assert s.liquidity_ok is True


def test_long_staying_is_cheaper_when_current_venue_pays_you():
    s = calc.switch_cost(
        "long", N, H, 0.00045, fills(0.0001, 0.0001), -0.00002,
        0.0005, fills(0.0002, 0.0002), 0.00001,
    )  # fmt: skip
    assert s.stay_total_cost_usd == pytest.approx(-67.2 + 4.5 + 1.0)  # funding received
    assert s.switch_total_cost_usd == pytest.approx(5.5 + 7 + 33.6 + 7)
    assert s.saving_usd == pytest.approx(-114.8)  # negative: staying is cheaper
    assert s.funding_advantage_per_hour_usd == pytest.approx(-0.3)
    assert s.break_even_hours is None  # no positive advantage, never pays back


def test_short_side_uses_the_buy_to_close_slippage_and_flips_funding():
    # short closes with a buy; Hyperliquid +0.0100%/h pays the short, Strike -0.0050%/h charges it
    s = calc.switch_cost(
        "short", N, H, 0.00045, fills(0.0003, 0.0001), 0.0001,
        0.0005, fills(0.0004, 0.0002), -0.00005,
    )  # fmt: skip
    assert s.stay_total_cost_usd == pytest.approx(-336 + 4.5 + 3.0)
    # close now 4.5 + 3.0 (buy), open 5 + 2.0 (sell), funding +168, close 5 + 4.0 (buy)
    assert s.switch_total_cost_usd == pytest.approx(7.5 + 7 + 168 + 9)
    assert s.saving_usd == pytest.approx(-520.0)
    assert s.funding_advantage_per_hour_usd == pytest.approx(-1.5)
    assert s.break_even_hours is None


def test_exit_on_the_current_venue_cancels_out_of_the_saving():
    for fee in (0.0, 0.00045, 0.01):
        s = calc.switch_cost(
            "long", N, H, fee, fills(0.0001, 0.0005), 0.0001,
            0.0005, fills(0.0002, 0.0002), -0.00005,
        )  # fmt: skip
        assert s.saving_usd == pytest.approx(336.0 - (5 + 2 + 5 + 2 - 168))  # fee-independent


def test_maker_mode_has_no_slippage_and_a_strike_rebate_pays_back_at_once():
    s = calc.switch_cost(
        "long", N, H, 0.00015, None, 0.0001, -0.00005, None, -0.00005, maker=True,
    )  # fmt: skip
    assert s.stay_total_cost_usd == pytest.approx(336 + 1.5)
    assert s.switch_total_cost_usd == pytest.approx(1.5 - 0.5 - 168 - 0.5)
    assert s.saving_usd == pytest.approx(505.0)
    assert s.break_even_hours == 0.0  # entry and exit on Strike are a net rebate


def test_missing_funding_or_thin_book_gives_none_never_zero():
    ok = fills(0.0001, 0.0001)
    miss = calc.switch_cost("long", N, H, 0.00045, ok, 0.0001, 0.0005, ok, None)
    assert miss.saving_usd is None and miss.switch_total_cost_usd is None
    assert miss.break_even_hours is None and miss.funding_advantage_per_hour_usd is None
    thin = calc.switch_cost("long", N, H, 0.00045, calc.FillEstimate(None, None, None, None),
                            0.0001, 0.0005, ok, -0.00005)  # fmt: skip
    assert thin.liquidity_ok is False and thin.saving_usd is None and thin.break_even_hours is None


# --- API ---------------------------------------------------------------------------------------
def get(c, qs):
    return c.get("/api/v1/switch?" + qs)


def test_api_defaults_and_shape_and_matches_the_cost_engine():
    app, c = make()
    d = get(c, "symbol=ETH&side=long&from_venue=hyperliquid").json()
    assert d["symbol"] == "ETH-USD" and d["hours"] == 336 and d["period_label"] == "2 weeks"
    assert d["notional_usd"] == 10_000 and d["assumptions"]["fee_mode"] == "taker"
    assert d["estimated_saving_usd"] == pytest.approx(d["stay_cost_usd"] - d["switch_cost_usd"])
    assert d["staying_is_cheaper"] is (d["estimated_saving_usd"] < 0)
    # rebuild from the engine so the endpoint is checked against the same math
    ctx = app.state.ctx
    m = ctx.engine.markets["ETH-USD"]
    res = ctx.engine.switch(m, "long", 10_000, 336, "taker", "hyperliquid")
    assert d["estimated_saving_usd"] == pytest.approx(res.cost.saving_usd)
    assert d["switching_cost_upfront_usd"] == pytest.approx(res.cost.upfront_cost_usd)
    items = {i["label"]: i["usd"] for i in d["switch_line_items"]}
    assert items["Cost of moving to Strike"] == pytest.approx(-d["switch_cost_usd"])
    assert sum(i["usd"] for i in d["switch_line_items"][:-1]) == pytest.approx(
        -d["switch_cost_usd"]
    )
    assert sum(i["usd"] for i in d["stay_line_items"][:-1]) == pytest.approx(-d["stay_cost_usd"])
    for k in ("stale", "stale_venues", "confidence", "confidence_reasons", "market_open", "disclaimer",
              "strike_cheaper_share_30d", "strike_cheaper_hours_compared_30d", "venues", "trade_url"):  # fmt: skip
        assert k in d, k
    assert 0 <= d["strike_cheaper_share_30d"] <= 1 and d["strike_cheaper_hours_compared_30d"] > 0


def test_api_share_matches_the_report_for_each_side_and_venue():
    _, c = make()
    rep = c.get("/api/v1/report").json()
    eth = next(m for m in rep["markets"] if m["symbol"] == "ETH-USD")
    for side, key in (("long", "long_strike_cheaper_pct"), ("short", "short_strike_cheaper_pct")):
        for venue in ("hyperliquid", "okx"):
            want = next(v[key] for v in eth["view_a"] if v["other_venue"] == venue)
            got = get(c, f"symbol=ETH-USD&side={side}&from_venue={venue}").json()
            assert got["strike_cheaper_share_30d"] == want


def test_api_summary_says_which_choice_is_cheaper():
    _, c = make()
    lng = get(c, "symbol=ETH-USD&side=long&from_venue=hyperliquid").json()
    sht = get(c, "symbol=ETH-USD&side=short&from_venue=hyperliquid").json()
    assert lng["staying_is_cheaper"] is False and sht["staying_is_cheaper"] is True
    assert lng["summary"].startswith("Moving to Strike is estimated to save about $")
    assert lng["summary"].endswith("over 2 weeks.")
    assert re.fullmatch(
        r"Staying on Hyperliquid is cheaper right now, by about \$[\d,.]+ over 2 weeks\.",
        sht["summary"],
    )
    assert sht["break_even_hours"] is None and lng["break_even_hours"] > 0


@pytest.mark.parametrize(
    ("hours", "label"),
    [
        (24, "1 day"),
        (72, "3 days"),
        (168, "1 week"),
        (336, "2 weeks"),
        (720, "1 month"),
        (36, "36 hours"),
    ],
)
def test_period_labels(hours, label):
    from app.api import period_label

    assert period_label(hours) == label


def test_api_errors_and_validation_use_the_standard_error_shape():
    _, c = make()
    bad = [
        ("symbol=ETH&side=long&from_venue=binance", 422),
        ("symbol=ETH&side=flat&from_venue=okx", 422),
        ("symbol=ETH&side=long", 422),
        ("symbol=ETH&side=long&from_venue=okx&hours=0", 422),
        ("symbol=ETH&side=long&from_venue=okx&notional=-5", 422),
        ("symbol=ETH&side=long&from_venue=okx&fee_mode=x", 422),
        ("symbol=NOPE&side=long&from_venue=okx", 404),
        ("symbol=NAS100&side=long&from_venue=hyperliquid", 400),  # OKX-only market
    ]
    for qs, code in bad:
        r = get(c, qs)
        assert r.status_code == code, qs
        assert set(r.json()) == {"error", "detail"}, qs


def test_api_is_rate_limited_like_the_other_v1_endpoints():
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.db import Store
    from app.main import create_app
    from tests.helpers import seed_history

    store = Store(":memory:")
    seed_history(store, WED_NOON_UTC)
    app = create_app(Settings(), store=store, state=build_state(WED_NOON_UTC),
                     now_ms=lambda: WED_NOON_UTC, start_background=False,
                     cache_ttl_seconds=30, rate_limit_per_minute=3)  # fmt: skip
    c = TestClient(app)
    qs = "symbol=ETH&side=long&from_venue=okx"
    assert [get(c, qs).status_code for _ in range(3)] == [200, 200, 200]
    r = get(c, qs)
    assert r.status_code == 429 and set(r.json()) == {"error", "detail"}
    assert "Retry-After" in r.headers


def test_stale_venue_is_flagged_and_lowers_confidence():
    state = build_state(WED_NOON_UTC)
    old = WED_NOON_UTC - 6 * 60_000
    state.quotes["okx"] = {s: replace(q, source_ts_ms=old) for s, q in state.quotes["okx"].items()}
    _, c = make(state=state)
    d = get(c, "symbol=ETH&side=long&from_venue=okx").json()
    assert d["stale"] is True and d["stale_venues"] == ["okx"]
    assert d["confidence"] != "High" and any("Stale" in r for r in d["confidence_reasons"])
    assert get(c, "symbol=ETH&side=long&from_venue=hyperliquid").json()["stale"] is False


def test_ticker_only_market_is_low_confidence_and_flagged():
    _, c = make()
    d = get(c, "symbol=DRAM&side=long&from_venue=hyperliquid").json()
    assert d["ticker_only"] is True and d["confidence"] == "Low"
    assert any("Ticker match only" in r for r in d["confidence_reasons"])
    assert get(c, "symbol=ETH&side=long&from_venue=hyperliquid").json()["ticker_only"] is False


def test_closed_market_forces_low_confidence():
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.db import Store
    from app.main import create_app
    from tests.helpers import seed_history

    sat = int(datetime(2026, 10, 24, 15, 30, tzinfo=UTC).timestamp() * 1000)  # a Saturday
    store = Store(":memory:")
    seed_history(store, sat)
    app = create_app(Settings(), store=store, state=build_state(sat), now_ms=lambda: sat,
                     start_background=False, cache_ttl_seconds=0, rate_limit_per_minute=100_000)  # fmt: skip
    c = TestClient(app)
    d = get(c, "symbol=NVDA&side=long&from_venue=hyperliquid").json()
    assert d["market_open"] is False and d["confidence"] == "Low"
    assert any("Underlying market closed" in r for r in d["confidence_reasons"])
    assert get(c, "symbol=ETH&side=long&from_venue=hyperliquid").json()["market_open"] is True


def test_hl_hip3_fee_unverified_lowers_confidence():
    _, c = make()
    d = get(c, "symbol=NVDA&side=long&from_venue=hyperliquid").json()
    assert d["hl_fee_unverified"] is True
    assert (
        get(c, "symbol=ETH&side=long&from_venue=hyperliquid").json()["hl_fee_unverified"] is False
    )


# --- the Move to Strike? tab ---------------------------------------------------------------------
def tab(c, qs=""):
    return c.get("/?tab=switch" + qs).text


def test_tab_is_fourth_after_only_on_strike_and_shows_the_spec_text():
    _, c = make()
    h = tab(c)
    labels = re.findall(r'<nav class="tabs".*?</nav>', h, re.S)[0]
    names = re.findall(r">([^<>]+?)(?: \(\d+\))?</a>", labels)
    assert names == [
        "Two-leg opportunities",
        "Cheapest to hold",
        "Only on Strike",
        "Move to Strike?",
    ]
    assert "<h2>Should I move my position to Strike?</h2>" in h
    assert ("Enter a position you already hold elsewhere. Strike Edge compares keeping it there "
            "with closing it and reopening on Strike, after fees, slippage and funding.") in h  # fmt: skip
    for label in ("Market", "I hold a", "Size (USD)", "Currently on", "Keeping it for"):
        assert label in h, label
    for preset in ("1 week", "2 weeks", "1 month"):
        assert (
            preset in h.split('<span class="seg-label">Keeping it for</span>')[1].split("</div>")[0]
        ), preset
    assert "I want to go" not in h  # the direction toggle is hidden on this tab
    assert 'aria-label="Controls"' not in h  # the generic controls are replaced
    assert "I want to go" in c.get("/").text  # but still on the other tabs


def test_tab_defaults_to_two_weeks_and_keeps_an_explicit_selection():
    _, c = make()
    h = tab(c, "&symbol=ETH-USD&side=long&from=hyperliquid")
    assert "Estimated saving over 2 weeks if you move to Strike" in h
    assert '<option value="ETH-USD" selected>' in h and '<option value="long" selected>' in h
    assert '<option value="hyperliquid" selected>' in h
    assert 'class="on" href="/?notional=10000&amp;hours=336' in h
    assert "sw-note" not in h.replace(".sw-note", "")  # no auto-pick line for an explicit URL


def test_tab_card_when_moving_saves_money():
    _, c = make()
    h = tab(c, "&symbol=ETH-USD&side=long&from=hyperliquid")
    api = get(c, "symbol=ETH-USD&side=long&from_venue=hyperliquid").json()
    saving = api["estimated_saving_usd"]
    assert f'<p class="sw-big pos">+${saving:,.2f}</p>' in h
    assert "Switching costs about $" in h and "(close on Hyperliquid, open on Strike)" in h
    assert "It pays for itself after about" in h
    assert "Stay on Hyperliquid" in h and "Move to Strike" in h and "you receive" in h
    assert "Strike cheaper for ETH longs in <b>" in h and "of hours" in h
    assert "Trade on Strike" in h and "Details and assumptions" in h
    for line in ("Close on Hyperliquid now (fee)", "Open on Strike now (fee)",
                 "Funding on Strike over 2 weeks", "Close on Strike at the end (slippage)",
                 "Cost of staying", "Staying on Hyperliquid instead"):  # fmt: skip
        assert line in h, line


def test_tab_card_when_staying_is_cheaper_is_plain_and_neutral():
    _, c = make()
    h = tab(c, "&symbol=ETH-USD&side=short&from=hyperliquid")
    assert '<p class="sw-big">Staying is cheaper</p>' in h and "sw-big pos" not in h
    assert "Staying on Hyperliquid is cheaper right now, by about $" in h
    assert "At current funding, moving does not pay for itself." in h
    assert "Strike cheaper for ETH shorts in <b>" in h  # the 30-day share is still shown


def test_tab_excludes_ticker_only_markets_by_default_and_can_show_them():
    _, c = make()
    options = lambda h: set(re.findall(r'<option value="([A-Z0-9]+-USD)"', h))  # noqa: E731
    default = options(tab(c))
    assert default and "DRAM-USD" not in default
    assert "ticker-only matches" in tab(c) and "Show them" in tab(c)
    shown = tab(c, "&unverified=1")
    assert "DRAM-USD" in options(shown) and "Hide them again" in shown
    # asking for a hidden market falls back to the default market instead of showing it
    assert '<option value="ETH-USD" selected>' in tab(c, "&symbol=DRAM-USD")
    d = tab(c, "&symbol=DRAM-USD&unverified=1")
    assert '<option value="DRAM-USD" selected>' in d
    assert '<span class="tag warn">Ticker match only</span>' in d and "Low" in d


def test_tab_market_list_follows_the_venue():
    _, c = make()
    on_okx = set(re.findall(r'<option value="([A-Z0-9]+-USD)"', tab(c, "&from=okx")))
    on_hl = set(re.findall(r'<option value="([A-Z0-9]+-USD)"', tab(c, "&from=hyperliquid")))
    assert {"NAS100-USD", "NIGHT-USD"} <= on_okx and not {"NAS100-USD", "NIGHT-USD"} & on_hl


def test_tab_has_no_overflow_markup_problems_and_other_tabs_are_unchanged():
    _, c = make()
    for qs in ("", "&side=short", "&from=okx&hours=720", "&notional=50000&fee=maker"):
        assert c.get("/?tab=switch" + qs).status_code == 200
    for q in ("/", "/?tab=hold", "/?tab=only", "/?tab=all", "/?tab=unranked"):
        assert "Should I move my position to Strike?" not in c.get(q).text


# --- polish: result label, auto-pick, top 5, secondary button ------------------------------------
TICKER = ("CXMT", "DRAM", "MINIMAX", "SPCX", "UNITREE", "ZHIPU")
NOTE_BEST = "Showing the position where moving to Strike saves the most right now."
NOTE_NONE = "Right now no verified position saves by moving to Strike."


def selected(h):
    """(symbol, side, venue) selected in the form."""
    opts = re.findall(r'<option value="([^"]+)" selected>', h)
    return opts[0], opts[1], opts[2]


def all_combos(c, qs=""):
    """Every market/side/venue from the API: used to find the true best independently."""
    snap = c.get("/api/v1/snapshot").json()["markets"]
    out = []
    for m in snap:
        if m["ticker_only"]:
            continue
        for venue in ("hyperliquid", "okx"):
            if venue not in m["venues"]:
                continue
            for side in ("long", "short"):
                d = get(c, f"symbol={m['symbol']}&side={side}&from_venue={venue}{qs}").json()
                if (
                    d["confidence"] != "Low"
                    and not d["stale"]
                    and d["estimated_saving_usd"] > 0.005
                ):
                    out.append((d["estimated_saving_usd"], m["symbol"], side, venue))
    return sorted(out, reverse=True)


def stale_strike_state():
    state = build_state(WED_NOON_UTC)
    old = WED_NOON_UTC - 6 * 60_000
    state.quotes["strike"] = {
        s: replace(q, source_ts_ms=old) for s, q in state.quotes["strike"].items()
    }
    return state


def test_label_says_saving_when_positive_and_result_when_staying_is_cheaper():
    _, c = make()
    pos = tab(c, "&symbol=ETH-USD&side=long&from=hyperliquid")
    neg = tab(c, "&symbol=ETH-USD&side=short&from=hyperliquid")
    assert "Estimated saving over 2 weeks if you move to Strike" in pos
    assert "Result over 2 weeks" not in pos
    assert "Result over 2 weeks" in neg
    assert "Estimated saving over" not in neg
    one_month = tab(c, "&symbol=ETH-USD&side=short&from=hyperliquid&hours=720")
    assert "Result over 1 month" in one_month


def test_auto_pick_selects_the_largest_verified_saving_and_says_so():
    _, c = make()
    best = all_combos(c)
    assert best, "the fixture set has positions where moving saves"
    h = tab(c)
    sym, side, venue = selected(h)
    assert (sym, side, venue) == best[0][1:]
    assert f'<p class="sw-note">{NOTE_BEST}</p>' in h and NOTE_NONE not in h
    assert sym.split("-")[0] not in TICKER  # ticker-only matches are never picked
    saving = get(c, f"symbol={sym}&side={side}&from_venue={venue}").json()["estimated_saving_usd"]
    assert f'<p class="sw-big pos">+${saving:,.2f}</p>' in h or f"+${saving:,.0f}" in h


def test_auto_pick_follows_the_selected_period_and_size_and_only_when_nothing_is_explicit():
    _, c = make()
    for qs in ("&hours=168", "&hours=720&notional=50000"):
        best = all_combos(
            c, "&" + qs[1:].replace("hours=", "hours=").replace("notional=", "notional=")
        )
        assert selected(tab(c, qs))[:3] == best[0][1:], qs
    # any explicit symbol, side or venue turns the auto-pick off and is respected as given
    for qs, want in (
        ("&symbol=SOL-USD", "SOL-USD"),
        ("&side=short", None),
        ("&from=okx", None),
    ):
        h = tab(c, qs)
        assert NOTE_BEST not in h and NOTE_NONE not in h, qs
        if want:
            assert selected(h)[0] == want


def test_preset_links_keep_the_auto_pick_but_freeze_an_explicit_selection():
    _, c = make()
    auto = tab(c).split('<span class="seg-label">Keeping it for</span>')[1].split("</div>")[0]
    assert "symbol=" not in auto and "side=" not in auto and "from=" not in auto
    assert "hours=168" in auto and "hours=720" in auto
    exp = tab(c, "&symbol=ETH-USD&side=short&from=okx")
    exp = exp.split('<span class="seg-label">Keeping it for</span>')[1].split("</div>")[0]
    assert "symbol=ETH-USD" in exp and "side=short" in exp and "from=okx" in exp


def test_auto_pick_falls_back_to_eth_long_on_hyperliquid_when_nothing_saves():
    _, c = make(state=stale_strike_state())  # nothing is verified when Strike data is stale
    h = tab(c)
    assert selected(h) == ("ETH-USD", "long", "hyperliquid")
    assert f'<p class="sw-note">{NOTE_NONE}</p>' in h and NOTE_BEST not in h
    assert "Where moving to Strike saves the most right now" not in h  # nothing to list


def test_top_five_lists_verified_positive_savings_with_links_to_each_selection():
    _, c = make()
    h = tab(c)
    assert '<h3 class="sw-top-title">Where moving to Strike saves the most right now</h3>' in h
    rows = re.findall(r'<li><a href="([^"]+)"[^>]*>(.*?)</a></li>', h, re.S)
    best = all_combos(c)
    assert 1 <= len(rows) <= 5 and len(rows) == min(5, len(best))
    savings = [
        float(re.search(r"\+\$([\d,.]+)", body).group(1).replace(",", "")) for _, body in rows
    ]
    assert savings == sorted(savings, reverse=True)
    for (href, body), (_want, sym, side, venue) in zip(rows, best, strict=False):
        qs = href.replace("&amp;", "&")
        assert qs.startswith("/?") and "tab=switch" in qs
        for part in (
            f"symbol={sym}",
            f"side={side}",
            f"from={venue}",
            "hours=336",
            "notional=10000",
            "fee=taker",
        ):
            assert part in qs, (part, qs)
        assert sym.split("-")[0] in body and f"{side} on " in body
        assert not any(f"<b>{tkr}</b>" in body for tkr in TICKER)
    # following a link selects exactly that position, and turns the auto-pick off
    href = rows[1][0].replace("&amp;", "&")
    page = c.get(href).text
    assert selected(page) == best[1][1:] and NOTE_BEST not in page


def test_top_five_shows_confidence_and_follows_the_selected_period():
    _, c = make()
    one_week = tab(c, "&hours=168")
    assert "hours=168" in re.findall(r'<li><a href="([^"]+)"', one_week)[0]
    assert "kept 1 week" in one_week
    assert re.search(r'<li><a .*?class="badge conf-(High|Medium)"', one_week, re.S)
    assert "conf-Low" not in one_week.split("sw-top-title")[1].split("</ol>")[0]


def test_trade_button_is_primary_when_saving_and_outline_when_staying_is_cheaper():
    _, c = make()
    pos = tab(c, "&symbol=ETH-USD&side=long&from=hyperliquid")
    neg = tab(c, "&symbol=ETH-USD&side=short&from=hyperliquid")
    sw = lambda h: re.search(r'<div class="sw-trade">(.*?)</div>', h, re.S).group(1)  # noqa: E731
    assert 'class="btn primary"' in sw(pos) and "outline" not in sw(pos)
    assert 'class="btn outline"' in sw(neg) and "primary" not in sw(neg)
    css = (ROOT / "app/static/app.css").read_text()
    assert ".btn.outline" in css


def test_links_into_the_tab_keep_the_auto_pick_on():
    _, c = make()
    home = c.get("/?tab=hold&side=short").text
    nav = re.search(r'<a [^>]*href="([^"]+)"[^>]*>Move to Strike\?</a>', home).group(1)
    nav = nav.replace("&amp;", "&")
    assert (
        "tab=switch" in nav and "side=" not in nav and "symbol=" not in nav and "from=" not in nav
    )
    assert NOTE_BEST in c.get(nav).text  # opening the tab from the tab bar picks the best position
    show = re.search(r'<a href="([^"]+)"[^>]*>Show them</a>', tab(c)).group(1).replace("&amp;", "&")
    assert "unverified=1" in show and "side=" not in show
    assert NOTE_BEST in c.get(show).text
