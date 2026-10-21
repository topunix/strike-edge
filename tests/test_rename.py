"""The product is Strike Edge. Marketing copy stays neutral; the data does not change."""

import re
from pathlib import Path

import yaml

from tests.test_web import make

ROOT = Path(__file__).resolve().parent.parent
# Built at runtime so this file does not contain the old names it forbids.
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
        low = text.lower()  # case-insensitive, so every capitalization is caught
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
    assert "<title>Strike Edge: find the better trade</title>" in html
    assert '<a class="brand" href="/">Strike Edge</a>' in html
    assert "Find the better trade." in html
    assert "Honest estimates after fees, funding and slippage" in html
    assert 'href="https://github.com/topunix/strike-edge"' in html
    assert "Strike Edge reads live funding" in html  # How it works
    assert "<title>Strike Edge report" in c.get("/report").text


def test_openapi_llms_and_deploy_files_use_the_new_name():
    _, c = make()
    spec = c.get("/openapi.json").json()
    assert spec["info"]["title"] == "Strike Edge"
    assert spec["info"]["description"].lstrip().startswith("Strike Edge makes Strike")
    llms = c.get("/llms.txt").text
    assert (
        llms.startswith("# Strike Edge")
        and "Honest estimates after fees, funding and slippage" in llms
    )
    svc = yaml.safe_load((ROOT / "render.yaml").read_text())["services"][0]
    assert svc["name"] == "strike-edge"
    assert 'name = "strike-edge"' in (ROOT / "pyproject.toml").read_text()
    readme = (ROOT / "README.md").read_text()
    assert readme.startswith("# Strike Edge\n") and "Strike Edge: find the better trade." in readme


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
