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
    assert f"**Live site:** {LIVE_URL}" in readme
    assert LIVE_URL in (ROOT / "docs" / "DEMO.md").read_text()
    top = "\n".join(readme.splitlines()[:10])
    assert (
        LIVE_URL in top and "![Dashboard on desktop]" in readme
    )  # URL and screenshot stay near the top


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


def test_readme_top_section_layout():
    lines = (ROOT / "README.md").read_text().splitlines()
    top = "\n".join(lines[:30])
    assert lines[0] == "# Strike Edge" and lines[2] == f"*{TAGLINE}*"
    assert lines[4] == INTRO and lines[6] == ESTIMATE_LINE  # intro, then the estimate line
    assert lines[8] == f"**Live site:** {LIVE_URL}"  # just the URL, no caveat
    assert "sleeps" not in top and "idle" not in top
    three = top.index("**Three ways to use it**")
    shot = top.index("![Dashboard on desktop](docs/screenshots/home-desktop.jpg)")
    assert (
        top.index("**Live site:**") < three < shot
    )  # list sits under the live line, above the image
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


def test_readme_ask_an_ai_uses_the_verified_live_numbers_only():
    readme = (ROOT / "README.md").read_text()
    sec = readme.split("## Ask an AI")[1].split("## Who it is for")[0]
    assert "Where is ETH cheapest to hold long for 1 week at $10k, after fees?" in sec
    for needle in ("+$22.57 net", "-$30.41", "$52.98", "On Strike", "2026-10-22", "get_holding_cost",
                   "Not financial advice", "Estimates after modeled fees"):  # fmt: skip
        assert needle in sec, needle
    import re

    assert sorted(set(re.findall(r"\$\d[\d,.]*", sec))) == [
        "$10",
        "$10,000",
        "$22.57",
        "$30.41",
        "$52.98",
    ]


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
