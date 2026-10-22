"""The product is Strike Edge. Marketing copy stays neutral; the data does not change."""

import re
from pathlib import Path

import yaml

from tests.test_web import make

ROOT = Path(__file__).resolve().parent.parent
# Built at runtime so this file does not contain the old names it forbids.
TAGLINE = "Find the better trade. Strike fast."  # the closing phrase is allowed only here
PHRASE = "strike" + " fast"  # runtime-built so this file never contains it
OLD = ["Strike" + " Fast", "strike" + "-fast", "strike" + "_fast", "strike" + "fast"]
TEXT_SUFFIXES = {".py", ".md", ".html", ".css", ".js", ".txt", ".yaml", ".yml", ".toml", ".json"}
WORSE = re.compile(r"strike is worse|sometimes strike|including when strike|even when strike", re.I)


def repo_text_files():
    for p in ROOT.rglob("*"):
        parts = set(p.parts)
        if not p.is_file() or parts & {".venv", ".git", "fixtures", "__pycache__", ".pytest_cache"}:
            continue
        if p.suffix in TEXT_SUFFIXES:
            yield p


def test_no_old_product_name_anywhere_in_the_repo():
    hits = []
    for p in repo_text_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        low = text.replace(TAGLINE, "").lower()  # tagline is the one allowed use; rest is scanned
        hits += [f"{p.relative_to(ROOT)}: {old}" for old in OLD if old.lower() in low]
    assert not hits, hits


def test_no_worse_phrasing_in_marketing_copy():
    _, c = make()
    pages = [c.get(path).text for path in ("/", "/?tab=hold", "/report", "/llms.txt")]
    pages += [(ROOT / "README.md").read_text(), (ROOT / "docs" / "DEMO.md").read_text()]
    for text in pages:
        assert not WORSE.search(text), WORSE.search(text).group(0)


def test_site_uses_the_new_name_tagline_and_repo_link():
    _, c = make()
    html = c.get("/").text
    assert f"<title>Strike Edge: {TAGLINE}</title>" in html
    assert '<a class="brand" href="/">Strike Edge</a>' in html
    assert f'<span class="tagline">{TAGLINE}</span>' in html
    assert "Honest estimates after fees, funding and slippage" in html
    assert 'href="https://github.com/topunix/strike-edge"' in html
    assert "Strike Edge reads live funding" in html  # How it works
    assert "<title>Strike Edge report" in c.get("/report").text


def test_openapi_llms_and_deploy_files_use_the_new_name():
    _, c = make()
    spec = c.get("/openapi.json").json()
    assert spec["info"]["title"] == "Strike Edge"
    assert "Strike Edge makes Strike" in spec["info"]["description"]
    llms = c.get("/llms.txt").text
    assert (
        llms.startswith("# Strike Edge")
        and "Honest estimates after fees, funding and slippage" in llms
    )
    svc = yaml.safe_load((ROOT / "render.yaml").read_text())["services"][0]
    assert svc["name"] == "strike-edge"
    assert 'name = "strike-edge"' in (ROOT / "pyproject.toml").read_text()
    readme = (ROOT / "README.md").read_text()
    assert readme.startswith("# Strike Edge\n") and f"*{TAGLINE}*" in readme


def test_unfavorable_results_are_still_shown():
    """Only copy changed: negative nets and 'did not' findings still appear as before."""
    _, c = make()
    day = c.get("/?hours=24").text
    assert "does not cover its costs" in day and "-$" in day
    rep = c.get("/api/v1/report").json()
    nets = [p["cum_net_30d_usd"] for m in rep["markets"] for p in m["pairs"]]
    assert nets and any(n < 0 for n in nets)  # losing directions are still in the tables
    opps = c.get("/api/v1/opportunities?hours=24").json()["opportunities"]
    assert any(o["net_usd"] < 0 for o in opps)


def test_logger_names_and_default_db_use_the_new_name():
    import inspect
    import logging

    import app.adapters.base as base
    import app.backfill as backfill
    import app.main as main
    import app.poller as poller
    from app.db import Store

    assert main.log.name == "strikeedge"
    assert poller.log.name == "strikeedge.poller"
    assert backfill.log.name == "strikeedge.backfill"
    assert base.log.name == "strikeedge.upstream"
    assert logging.getLogger("strikeedge.poller").parent.name == "strikeedge"
    assert inspect.signature(Store.__init__).parameters["path"].default == "strikeedge.db"
    assert inspect.signature(main.create_app).parameters["db_path"].default == "strikeedge.db"
    readme = (ROOT / "README.md").read_text()
    assert "`strikeedge.db`" in readme and "| `DB_PATH` | `strikeedge.db` |" in readme


OLD_TAGLINE = re.compile(
    r"find the better trade(?!\. " + "Strike" + r" fast\.)", re.I
)  # runtime-built


def test_tagline_is_everywhere_and_the_old_one_is_gone():
    from app.config import TAGLINE as CONFIG_TAGLINE

    assert CONFIG_TAGLINE == TAGLINE
    _, c = make()
    spec = c.get("/openapi.json").json()
    surfaces = {
        "home header": c.get("/").text,
        "report header": c.get("/report").text,
        "hold header": c.get("/?tab=hold").text,
        "llms.txt": c.get("/llms.txt").text,
        "openapi description": spec["info"]["description"],
        "README": (ROOT / "README.md").read_text(),
        "DEMO": (ROOT / "docs" / "DEMO.md").read_text(),
    }
    for name, text in surfaces.items():
        assert TAGLINE in text, name
        assert not OLD_TAGLINE.search(text), f"old tagline still in {name}"
    assert c.get("/llms.txt").text.splitlines()[0:3] == ["# Strike Edge", "", TAGLINE]
    assert 'class="tagline"' in surfaces["report header"]


def test_the_tagline_is_the_only_place_its_closing_phrase_appears():
    allowed = re.compile(re.escape(TAGLINE))
    for p in repo_text_files():
        rest = allowed.sub("", p.read_text(encoding="utf-8", errors="ignore"))
        assert PHRASE not in rest.lower(), p.relative_to(ROOT)
    _, c = make()
    for path in ("/", "/report", "/llms.txt"):
        assert PHRASE not in allowed.sub("", c.get(path).text).lower(), path


LIVE_URL = "https://strike-edge.onrender.com"
PLACEHOLDER = "YOUR" + "-APP"  # runtime-built so this file does not contain it


def test_live_url_replaces_the_placeholder_everywhere():
    for p in repo_text_files():
        assert PLACEHOLDER not in p.read_text(encoding="utf-8", errors="ignore"), p.relative_to(
            ROOT
        )
    readme = (ROOT / "README.md").read_text()
    assert "**Live site:**" not in readme  # the dashboard URL lives in "Three ways to use it"
    assert LIVE_URL in (ROOT / "docs" / "DEMO.md").read_text()
    top = "\n".join(readme.splitlines()[:16])
    assert f"- **Dashboard:** {LIVE_URL}\n" in top and "![Dashboard on desktop]" in readme


def test_readme_states_rate_limit_is_best_effort_and_todo_note_exists():
    readme = (ROOT / "README.md").read_text()
    line = (
        "Rate limiting is best-effort per client; it does not affect upstream API usage, "
        "which is fixed by the poller schedule."
    )
    assert line in readme
    assert (
        readme.index("## API quickstart")
        < readme.index(line)
        < readme.index("## How existing tools compare")
    )
    todo = (ROOT / "docs" / "TODO-rate-limit-client-key.md").read_text()
    for needle in (
        "CF-Connecting-IP",
        "rightmost",
        "trusted edge",
        "first entry",
        "Not a GitHub issue",
    ):
        assert needle in todo or needle.lower() in todo.lower(), needle


# --- README structure (top section, MCP, deploy branch) -----------------------------------------
BRANCH_WORD = "m" + "vp"  # runtime-built so this file never contains the deleted branch name
INTRO = (
    "Strike Edge answers one question: after funding, fees, spread and slippage, is there a real "
    "economic reason to trade a perp on Strike instead of Hyperliquid or OKX? It reads live "
    "funding, prices and order books from all three venues and serves honest after-cost "
    "estimates through a web dashboard, a JSON API, and an MCP server for AI agents."
)
ESTIMATE_LINE = (
    'Everything is an **estimate**, never "profit" or "guaranteed arbitrage". Information only: '
    "no trading, no wallets, no logins, no user funds."
)


def readme_text(lines):
    return "\n".join(lines)


def test_readme_top_section_layout():
    lines = (ROOT / "README.md").read_text().splitlines()
    top = "\n".join(lines[:30])
    assert lines[0] == "# Strike Edge" and lines[2] == f"*{TAGLINE}*"
    assert lines[4] == INTRO and lines[6] == ESTIMATE_LINE  # intro, then the estimate line
    assert lines[8] == "**Three ways to use it**"  # straight after the estimate line
    assert "**Live site:**" not in readme_text(lines)
    assert "sleeps" not in top and "idle" not in top
    three = top.index("**Three ways to use it**")
    shot = top.index("![Dashboard on desktop](docs/screenshots/home-desktop.jpg)")
    assert three < shot  # the list sits above the screenshot
    block = top[three:shot]
    assert f"- **Dashboard:** {LIVE_URL}\n" in block
    assert f"{LIVE_URL}/docs" in block and f"{LIVE_URL}/llms.txt" in block
    assert f"{LIVE_URL}/mcp" in block
    assert f"claude mcp add --transport http strike-edge {LIVE_URL}/mcp" in block
    assert "(#use-with-ai-agents-mcp)" in block
    readme = "\n".join(lines)
    assert "## Use with AI agents (MCP)" in readme  # the anchor target exists
    assert (
        readme.index("**Three ways to use it**")
        < readme.index("## Ask an AI")
        < readme.index("## Who it is for")
    )
    assert readme.index("![Dashboard on desktop]") < readme.index(
        "## Ask an AI"
    )  # after the screenshot


def test_readme_ask_an_ai_is_the_report_based_example_with_verified_numbers_only():
    import re

    sec = (ROOT / "README.md").read_text().split("## Ask an AI")[1].split("## Who it is for")[0]
    assert "calling the `get_report` tool" in sec
    assert (
        "Over the last 30 days, was Strike cheaper than Hyperliquid and OKX for holding ETH longs?"
        in sec
    )
    # the only numbers: the two verified live shares (84% vs Hyperliquid, 73% vs OKX), the date,
    # the window and its 720 hours, and the 30 day period
    assert sorted(set(re.findall(r"\d+(?:\.\d+)?%", sec))) == ["73%", "84%"]
    assert "**84%** of hours versus Hyperliquid and about **73%** versus OKX" in sec
    assert not re.search(r"\$\d", sec), (
        "no dollar figures: the example is report-based, not a live price"
    )
    for needle in ("2026-10-22 at 15:49 UTC", "window 2026-09-22 15:00 to 2026-10-22 15:00 UTC", "720 hours",
                   "Funding only", "past funding does not predict future funding", "not financial advice",
                   "`get_holding_cost` can differ", "single hour can go the other way"):  # fmt: skip
        assert needle in sec, needle
    # the old live-price example (and its funding-sign aside) is gone
    for gone in ("+$22.57", "-$30.41", "$52.98", "its funding was negative, so longs were paid"):
        assert gone not in (ROOT / "README.md").read_text(), gone


def test_readme_mentions_mcp_in_audience_and_comparison_and_main_for_deploys():
    readme = (ROOT / "README.md").read_text()
    who = readme.split("## Who it is for")[1].split("## Headline findings")[0]
    assert "MCP server at `/mcp`" in who and "(#use-with-ai-agents-mcp)" in who
    assert "an MCP server for AI agents, and honest per-row confidence" in readme
    assert "(for example `main`)" in readme
    assert "sleeps after about 15 minutes idle" in readme.split("## Deploy to Render")[1]


def test_no_file_refers_to_a_deleted_mvp_branch():
    import re

    word = re.compile(rf"\b{BRANCH_WORD}\b", re.I)
    hits = [
        str(p.relative_to(ROOT))
        for p in repo_text_files()
        if word.search(p.read_text(errors="ignore"))
    ]
    assert hits == [], hits
    assert "Deploy from the main branch." in (ROOT / "render.yaml").read_text()


def test_sleep_caveat_lives_only_in_the_deploy_section():
    readme = (ROOT / "README.md").read_text()
    before_deploy = readme.split("## Deploy to Render")[0]
    for phrase in ("Free hosting note", "sleeps", "idle", "wakes up", "free hosting"):
        assert phrase.lower() not in before_deploy.lower(), phrase
    deploy = readme.split("## Deploy to Render")[1].split("## Tests and tooling")[0]
    assert "sleeps after about 15 minutes idle" in deploy and "re-backfills" in deploy
    assert "Open the site 5 or more minutes before a demo" in deploy


def test_whats_next_section_is_short_plans_not_promises():
    import re

    readme = (ROOT / "README.md").read_text()
    assert "## What's next" in readme
    # placed just before the disclaimer section, with no other heading in between
    nxt = readme.index("## What's next")
    disc = readme.index("## Restricted jurisdictions and disclaimer")
    assert nxt < disc and "\n## " not in readme[nxt + 5 : disc]
    sec = readme[nxt:disc]
    assert "plans, not promises" in sec and "None of them is built yet" in sec
    assert "https://docs.strikefinance.org/api/builder-codes" in sec
    for needle in (
        "Trade this on Strike", "both legs of a two-leg trade", "maximum fee", "wallet signature",
        "control of withdrawals", "only launch where permitted", "confirming the terms with Strike",
        "Lighter and Aster first", "Apex, Pacifica and Paradex", "same accuracy checks",
        "Telegram or Discord", "verified opportunity crosses a threshold",
        "more than 30 days", "survives restarts", "docs/TODO-rate-limit-client-key.md",
    ):  # fmt: skip
        assert needle in sec, needle
    assert len(re.findall(r"^- \*\*", sec, flags=re.M)) == 5  # five short items
    assert len(sec.split()) < 260  # short
    assert "(docs/TODO-rate-limit-client-key.md)" in sec
    assert (ROOT / "docs" / "TODO-rate-limit-client-key.md").exists()
    # nothing in it claims the feature exists today
    for claim in ("now available", "is available today", "you can now trade", "already supports"):
        assert claim not in sec.lower(), claim


def test_demo_close_mentions_builder_codes_and_stays_within_time():
    import re

    demo = (ROOT / "docs" / "DEMO.md").read_text()
    close = demo.split("Close (last few seconds, one breath):")[1].split("\n")[0]
    assert (
        "Next: trading straight from an opportunity through Strike's builder codes, plus more venues and alerts."
        in close
    )
    assert TAGLINE in close and "Estimates only, not financial advice" in close
    assert (
        "Not yet. Trading from an opportunity through Strike's builder codes is a plan, not a feature."
        in demo
    )
    stamps = [tuple(map(int, m.split(":"))) for m in re.findall(r"\b(\d:\d\d)\b", demo)]
    assert max(stamps) <= (2, 20) and "under the 2.5 minute cap" in demo


def jpeg_size(path):
    """(width, height) of a JPEG, read from its header (no imaging library needed)."""
    import struct

    data = Path(path).read_bytes()
    assert data[:2] == b"\xff\xd8", f"{path} is not a JPEG"
    i = 2
    while i < len(data):
        assert data[i] == 0xFF, "bad JPEG marker"
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start-of-frame
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    raise AssertionError("no frame header")


def readme_images():
    import re

    text = (ROOT / "README.md").read_text()
    refs = set(re.findall(r"!\[[^\]]*\]\((docs/[^)\s]+)\)", text))
    refs |= set(re.findall(r'<img[^>]*src="(docs/[^"]+)"', text))
    refs |= set(re.findall(r"\]\((docs/screenshots/[^)\s]+)\)", text))
    return refs


def test_every_image_referenced_in_the_readme_exists():
    refs = readme_images()
    assert len(refs) >= 7
    assert [r for r in sorted(refs) if not (ROOT / r).is_file()] == []


def test_readme_screenshots_block_is_collapsible_with_three_mobile_images():
    import re

    readme = (ROOT / "README.md").read_text()
    sec = readme.split("## Screenshots")[1].split("## What it shows")[0]
    assert sec.count("<details>") == 1 and sec.count("</details>") == 1
    assert "<summary><b>Screenshots (mobile and desktop)</b></summary>" in sec
    assert "| --- |" not in sec  # the old table is gone
    imgs = re.findall(r'<img src="(docs/screenshots/[^"]+)" width="240" alt="([^"]+)"', sec)
    assert [src for src, _ in imgs] == [
        "docs/screenshots/home-mobile.jpg",
        "docs/screenshots/home-hold-mobile.jpg",
        "docs/screenshots/report-charts-mobile.jpg",
    ]
    assert all(len(alt) > 20 for _, alt in imgs)  # real alt text
    row_end = sec.index("</p>")
    caption = "*Mobile, 375x812 viewport: dashboard, cheapest to hold, and the report charts.*"
    assert sec.index(caption) > row_end  # one-line caption under the row of images
    desktop = (
        "Desktop: [report](docs/screenshots/report-desktop.jpg) and "
        "[cheapest to hold](docs/screenshots/home-hold-desktop.jpg)."
    )
    assert desktop in sec and "scripts/check_layout.py" in sec
    assert sec.index(caption) < sec.index(desktop) < sec.index("</details>")
    assert 'alt="' in sec and "<img" not in readme.split("## Screenshots")[0]  # no raw img above


def test_desktop_hero_and_charts_images_are_unchanged_in_place():
    readme = (ROOT / "README.md").read_text()
    top = readme.split("## Ask an AI")[0]
    assert "![Dashboard on desktop](docs/screenshots/home-desktop.jpg)" in top
    findings = readme.split("## Headline findings")[1].split("## Screenshots")[0]
    assert "(docs/screenshots/report-charts-desktop.jpg)" in findings
    assert jpeg_size(ROOT / "docs/screenshots/home-desktop.jpg")[0] == 1280


def test_mobile_screenshots_are_viewport_only_375_by_812():
    for name in ("home-mobile", "home-hold-mobile", "report-charts-mobile", "report-mobile"):
        assert jpeg_size(ROOT / f"docs/screenshots/{name}.jpg") == (375, 812), name
    # the hold shot is not a duplicate of the home shot (its first screen shows the hold view)
    home = (ROOT / "docs/screenshots/home-mobile.jpg").read_bytes()
    hold = (ROOT / "docs/screenshots/home-hold-mobile.jpg").read_bytes()
    assert home != hold
