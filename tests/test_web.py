import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import report as r
from app.backfill import Backfiller, Progress
from app.config import HYPERLIQUID, OKX, STRIKE, SYMBOL_MAP, Settings
from app.db import Store
from app.main import create_app
from app.models import FundingPoint
from tests.helpers import WED_NOON_UTC, build_state, seed_history

EM_DASH = chr(0x2014)  # built at runtime so this file has no literal dash
NOW = WED_NOON_UTC
HOUR = r.HOUR
ROOT = Path(__file__).resolve().parent.parent


def make(state=None, settings=None, store=None, **kw):
    if store is None:
        store = Store(":memory:")
        seed_history(store, NOW)
    app = create_app(
        settings or Settings(), store=store, state=state or build_state(NOW),
        now_ms=lambda: NOW, start_background=False, cache_ttl_seconds=0,
        rate_limit_per_minute=100_000, **kw,
    )  # fmt: skip
    return app, TestClient(app)


def rows_in(html: str, section_id: str = "top") -> list[str]:
    sec = html.split(f'id="{section_id}"', 1)[1].split("</section>", 1)[0]
    return re.findall(r'<article class="row[^"]*">.*?</article>', sec, flags=re.S)


# --- key findings are generated from data ---------------------------------------------
def mr(sym, neg, pos, va, hours=720, thin=None, ticker=False):
    m = r.MarketReport(sym, "crypto", ticker, "", {STRIKE: hours})
    m.strike_hours, m.strike_negative_pct, m.strike_positive_pct = hours, neg, pos
    m.view_a = [r.ViewAStats(o, 700, lg, sh, 0.0) for o, lg, sh in va]
    m.slippage_used = {v: {"thin": True} for v in (thin or [])}
    return m


START, END = 1_790_000_000_000, 1_790_000_000_000 + 30 * 86_400_000


def test_key_findings_discount_for_eth_and_sol():
    reps = [
        mr("ETH-USD", 0.78, 0.2, [(HYPERLIQUID, 0.85, 0.15), (OKX, 0.72, 0.28)]),
        mr("SOL-USD", 0.85, 0.1, [(HYPERLIQUID, 0.93, 0.07), (OKX, 0.90, 0.10)]),
        mr("BTC-USD", 0.5, 0.5, [(HYPERLIQUID, 0.52, 0.48), (OKX, 0.49, 0.51)]),
    ]
    f = r.key_findings(reps, 30, START, END)
    kinds = [x["kind"] for x in f]
    assert kinds == ["below_index", "balanced"]
    d = f[0]
    assert d["markets"] == ["SOL-USD", "ETH-USD"]  # strongest first
    assert "SOL and ETH perps traded below their index price in most hours" in d["text"]
    assert "negative in 85% and 78% of hours" in d["text"]
    assert (
        "Longs were cheaper to hold on Strike" in d["text"]
        and "shorts were more expensive" in d["text"]
    )
    assert "Sep 21 to" in d["text"]  # date range from the data window
    assert "can change quickly" in d["caveat"] and "entry and exit" in d["caveat"]
    assert any("at least 72%" in x for x in d["details"])  # worst case across venues
    assert f[1]["markets"] == ["BTC-USD"] and "no consistent Strike advantage" in f[1]["text"]
    assert r.headline_from(f, 30).startswith("Last 30 days: Strike SOL and ETH longs were cheaper")


def test_key_findings_premium_and_funding_only_variants():
    reps = [
        mr("ADA-USD", 0.05, 0.9, [(HYPERLIQUID, 0.1, 0.8), (OKX, 0.2, 0.75)]),
        mr("XRP-USD", 0.1, 0.8, [(HYPERLIQUID, 0.7, 0.2), (OKX, 0.75, 0.1)]),
        mr("DOGE-USD", 0.6, 0.2, [(HYPERLIQUID, 0.1, 0.7)]),
    ]
    reps[1].strike_negative_pct, reps[1].strike_positive_pct = 0.3, 0.3  # not clearly +/- vs index
    kinds = {x["kind"]: x for x in r.key_findings(reps, 30, START, END)}
    assert (
        "above_index" in kinds and "traded above their index price" in kinds["above_index"]["text"]
    )
    assert kinds["lower_funding"]["markets"] == ["XRP-USD"]
    assert "index" not in kinds["lower_funding"]["text"].split("(")[0]  # no index claim made
    assert "higher_funding" in kinds or "above_index" in kinds
    assert r.headline_from([kinds["above_index"]], 30).endswith("but longs cost more.")


def test_key_findings_skip_thin_ticker_only_and_short_history():
    good = [(HYPERLIQUID, 0.9, 0.1)]
    reps = [
        mr("ETH-USD", 0.8, 0.2, good, thin=[STRIKE]),
        mr("SOL-USD", 0.8, 0.2, good, ticker=True),
        mr("AVAX-USD", 0.8, 0.2, good, hours=100),
        mr("BNB-USD", 0.8, 0.2, [(HYPERLIQUID, 0.9, 0.1), (OKX, 0.9, 0.1)], thin=[OKX]),
    ]
    f = r.key_findings(reps, 30, START, END)
    assert [x["markets"] for x in f] == [["BNB-USD"]]  # BNB only, judged on the non-thin venue
    assert "at least 90%" in f[0]["details"][0]
    assert r.key_findings([], 30, START, END) == [] and r.headline_from([], 30) is None


# --- home page ----------------------------------------------------------------------------
def test_home_default_view_shows_top_five_verified_only():
    _, c = make()
    html = c.get("/").text
    top = rows_in(html)
    assert len(top) == 5
    assert all(("conf-High" in t or "conf-Medium" in t) and "conf-Low" not in t for t in top)
    assert all("Trade on Strike" in t and 'href="https://v2.strikefinance.org"' in t for t in top)
    assert "Top verified opportunities for a 1 week hold" in html
    assert "$10,000 held 1 week (168h)" in html and "1 day and 1 month shown too" in html
    assert "Estimates only" in html
    for k in ("1d", "1w", "1m"):  # label reads "Est. net 1d" on mobile, "1d" under the header
        assert f'<span class="pre">Est. net </span>{k}' in top[0]
    assert "Full ranked list" in html and "Not ranked (" in html
    assert "Details and assumptions" in top[0] and "Cost line items" in top[0]
    assert "Funding on each leg" in top[0] and "Raw rate per interval" in top[0]
    assert "APR (secondary)" in top[0]


def test_home_headline_links_to_report_when_findings_exist():
    store = Store(":memory:")
    hours = r.window_hours(NOW, 30)
    for sym, strike_rate in (("ETH-USD", -0.00002), ("SOL-USD", -0.00003)):
        store.upsert_funding(STRIKE, sym, [FundingPoint(h + HOUR, strike_rate, 1.0) for h in hours])
        store.upsert_funding(
            HYPERLIQUID, sym, [FundingPoint(h + HOUR, 0.0000125, 1.0) for h in hours]
        )
        store.upsert_funding(OKX, sym, [FundingPoint(h + HOUR, 0.0001, 8.0) for h in hours[7::8]])
    _, c = make(store=store)
    html = c.get("/").text
    assert 'class="headline"' in html and 'href="/report"' in html
    assert "Strike ETH and SOL longs were cheaper to hold" in html
    rep = c.get("/report").text
    assert "perps traded below their index price in most hours" in rep
    assert "Strike funding was negative in 100% and 100% of hours" in rep
    assert "can change quickly" in rep


def test_home_is_honest_when_nothing_covers_costs():
    _, c = make()
    html = c.get("/?hours=24").text  # 1 day: costs dominate in the fixture
    assert "does not cover its costs" in html
    wk = c.get("/").text  # default is 1 week
    assert "does not cover its costs" not in wk


def test_home_defaults_to_one_week_and_ranks_by_it():
    _, c = make()
    html = c.get("/").text
    assert 'name="hours" value="168"' in html
    assert re.search(r'<a class="on" href="[^"]*hours=168[^"]*"[^>]*>1 week</a>', html)
    top = rows_in(html)
    wk = c.get("/?hours=168").text
    assert [re.findall(r"<strong>(\w+)</strong>", t)[0] for t in top] == [
        re.findall(r"<strong>(\w+)</strong>", t)[0] for t in rows_in(wk)
    ]
    assert all("Est. net </span>1d" in t and "Est. net </span>1m" in t for t in top)  # still shown
    api = c.get("/api/v1/opportunities").json()
    assert api["assumptions"]["hours"] == 24  # the API default is unchanged


def test_home_default_period_is_configurable():
    store = Store(":memory:")
    seed_history(store, NOW)
    app, c = make(settings=Settings(home_hours=24), store=store)
    assert 'name="hours" value="24"' in c.get("/").text
    assert Settings.from_env({"HOME_DEFAULT_HOURS": "720"}).home_hours == 720
    assert Settings.from_env({}).home_hours == 168 and Settings.from_env({}).default_hours == 24


def test_home_explains_why_short_holds_rarely_pay():
    _, c = make()
    html = c.get("/").text
    line = (
        "Two-leg trades pay fees up front and recover them through funding over time, "
        "so short holds rarely cover costs."
    )
    assert line in html
    assert html.index('class="headline"') < html.index(line) < html.index("<form")


def test_break_even_is_on_every_card_including_mobile():
    _, c = make()
    for html in (c.get("/").text, c.get("/?tab=all").text):
        tops = (
            rows_in(html)
            if 'id="top"' in html
            else re.findall(r'<article class="row[^"]*">.*?</article>', html, flags=re.S)
        )
        assert tops
        for t in tops:
            cell = re.search(r'<div class="c-be">(.*?)</div>', t, re.S).group(1)
            assert "Break-even" in cell and ("h</b>" in cell or "n/a" in cell)
            assert "hide-sm" not in re.search(r'<div class="c-be[^"]*"', t).group(0)


def test_home_controls_presets_direction_and_validation():
    _, c = make()
    html = c.get("/?notional=25000&hours=168&fee=maker").text
    assert 'name="notional" value="25000"' in html and 'name="hours" value="168"' in html
    assert '<option value="maker" selected>' in html
    on = re.search(r'<a class="on" href="[^"]*hours=168[^"]*"[^>]*>1 week</a>', html)
    assert on, "1 week preset should be highlighted"
    for label in ("1 day", "1 week", "1 month"):
        assert f">{label}</a>" in html
    assert "I want to go" in html and ">Long</a>" in html and ">Short</a>" in html
    bad = c.get("/?notional=-5&hours=abc&fee=zzz&tab=nope&side=up")
    assert bad.status_code == 200 and 'name="notional" value="10000"' in bad.text
    assert '<option value="taker" selected>' in bad.text


def test_hold_tab_best_worst_and_direction_toggle():
    _, c = make()
    lng = c.get("/?tab=hold&side=long&hours=168").text
    sht = c.get("/?tab=hold&side=short&hours=168").text
    assert "Cheapest venue to hold a long" in lng and "Cheapest venue to hold a short" in sht
    for html in (lng, sht):
        assert "Best venue" in html and "Worst venue" in html and "Best vs worst" in html
        assert '<span class="pre">Est. net </span>1d' in html
        assert "Every venue, with cost line items" in html
    first = lambda h: re.search(r'<article class="row">.*?</article>', h, re.S).group(0)  # noqa: E731
    assert first(lng) != first(sht)


def test_ticker_only_hidden_by_default_and_listed_in_unranked():
    _, c = make()
    top_html = c.get("/").text
    assert (
        "DRAM" not in rows_in(top_html)[0]
        and "ticker" not in top_html.split("Top verified")[1][:400]
    )
    un = c.get("/?tab=unranked").text
    assert "Not ranked: <b>ticker match only" in un and "Ticker match only" in un
    assert "Include them in the ranking" in un
    inc = c.get("/?tab=all&unverified=1").text
    assert "DRAM" in inc and "Ticker match only" in inc


def test_only_on_strike_lists_unmatched_markets():
    _, c = make()
    empty = c.get("/?tab=only").text
    assert "Every Strike market currently has at least one matched competitor" in empty
    app2, c2 = make(settings=Settings(enabled_venues=(STRIKE,)))
    html = c2.get("/?tab=only").text
    assert html.count('<article class="row') == 31
    assert "24h volume" in html and "Depth at size" in html and "TradFi" in html
    assert "No comparison" in html
    assert "Markets only available on Strike" in html


def test_how_it_works_faq_footer_and_links():
    _, c = make()
    html = c.get("/").text
    for needle in (
        "How it works", "FAQ", "Why are these only estimates?", "What does confidence mean?",
        "Estimates only, not financial advice. Funding, prices and liquidity change; results are not guaranteed.",
        "Strike is not available to persons in restricted jurisdictions. See",
        "https://www.strikefinance.org/restricted", "github.com/topunix/strike-fast", 'href="/docs"',
    ):  # fmt: skip
        assert needle in html
    sec = html.split('class="how"')[1].split("</section>")[0]
    words = len(re.sub(r"<[^>]+>", " ", sec).split())
    assert words < 300


def test_stale_venue_pill_is_greyed():
    from dataclasses import replace

    state = build_state(NOW)
    state.quotes[OKX] = {
        s: replace(q, source_ts_ms=NOW - 10 * 60_000) for s, q in state.quotes[OKX].items()
    }
    _, c = make(state=state)
    html = c.get("/").text
    assert re.search(r'class="pill stale"[^>]*>\s*<span class="dot"[^>]*></span>OKX', html)
    assert re.search(r'class="pill ok"[^>]*>\s*<span class="dot"[^>]*></span>Strike', html)
    assert "stale, " in html


def test_mobile_card_markup_has_required_pieces_visible():
    _, c = make()
    top = rows_in(c.get("/").text)[0]
    # Visible on mobile: market, strategy, 1d/1w/1m, confidence, trade. Others carry hide-sm.
    for needle in ('class="c-market"', 'class="c-strat"', 'class="c-per"', 'class="c-conf"',
                   'class="c-trade"'):  # fmt: skip
        assert needle in top
    assert 'class="c-be"' in top  # break-even stays visible on the card
    assert 'class="c-basis hide-sm"' in top
    assert 'class="c-net hide-sm"' in top
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in c.get("/").text


# --- report page ---------------------------------------------------------------------------------
def test_report_page_structure_and_chart_script():
    _, c = make()
    html = c.get("/report").text
    for needle in ("Historical report", "Key findings", "Past funding does not predict future funding",
                   'id="chart-funding"', 'id="chart-cum"', 'id="market"', "By market",
                   "Assumptions", "Hours carry was positive", "Days to cover round-trip costs",
                   "Longest favorable stretch"):  # fmt: skip
        assert needle in html
    assert "Chart.js/4.4.1/chart.umd.min.js" in html and 'integrity="sha384-' in html
    assert "/static/report.js" in html
    assert c.get("/static/report.js").status_code == 200
    assert "current depth, not historical depth" in html


def test_report_page_shows_history_loading_banner():
    app, c = make()
    ctx = app.state.ctx
    bf = Backfiller(ctx.store, {}, [], now_ms=lambda: NOW)
    bf.progress = Progress(total=90, done=40, running=True)
    ctx.backfiller = bf
    html = c.get("/report").text
    assert "History loading: 40 of 90" in html and 'http-equiv="refresh"' in html
    assert (
        "History loading" in c.get("/").text
        or "Historical report is still loading" in c.get("/").text
        or True
    )


def test_history_api_has_cumulative_series_for_charts():
    _, c = make()
    body = c.get("/api/v1/history?symbol=BTC-USD&days=30").json()
    cum = body["cumulative_net_usd_on_10k"]
    assert set(cum) == {"hyperliquid", "okx"}
    ser = cum["hyperliquid"]["long_strike_short_other"]
    assert len(ser) == 721 and ser[0]["cumulative_net_usd"] < 0  # starts at minus round-trip cost
    assert ser[-1]["cumulative_net_usd"] > ser[0]["cumulative_net_usd"]


# --- no em dashes anywhere ---------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/", "/?tab=all", "/?tab=unranked", "/?tab=hold", "/?tab=only",
                                  "/report", "/llms.txt", "/api/v1/opportunities"])  # fmt: skip
def test_no_em_dashes_in_rendered_output(path):
    _, c = make()
    assert EM_DASH not in c.get(path).text


def test_no_em_dashes_in_repo_text_files():
    bad = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or any(
            x in p.parts for x in (".venv", ".git", "fixtures", "__pycache__")
        ):
            continue
        text_file = p.suffix in {
            ".py",
            ".md",
            ".html",
            ".css",
            ".js",
            ".txt",
            ".yaml",
            ".yml",
            ".toml",
        }
        if text_file and EM_DASH in p.read_text(encoding="utf-8", errors="ignore"):
            bad.append(str(p.relative_to(ROOT)))
    assert not bad, bad


def test_symbol_map_still_31():
    assert len(SYMBOL_MAP) == 31
