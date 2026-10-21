"""Playwright mobile and desktop layout check on fixture data. Skipped without a browser."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

pytest.importorskip("playwright.sync_api")
from app.config import Settings  # noqa: E402
from app.db import Store  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.browser_server import serve  # noqa: E402
from tests.helpers import WED_NOON_UTC, build_state, seed_history  # noqa: E402

pytestmark = pytest.mark.browser


def test_every_page_at_375_and_1280_has_no_overflow_and_charts_render(tmp_path):
    import check_layout
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            check_layout.launch(p).close()
    except Exception as exc:  # no usable chromium in this environment
        pytest.skip(f"no browser available: {exc}")

    store = Store(":memory:")
    seed_history(store, WED_NOON_UTC)
    app = create_app(
        Settings(), store=store, state=build_state(WED_NOON_UTC), now_ms=lambda: WED_NOON_UTC,
        start_background=False, cache_ttl_seconds=0, rate_limit_per_minute=100_000,
    )  # fmt: skip
    with serve(app) as base:
        problems = check_layout.run(base, tmp_path)
    assert problems == []
    shots = sorted(p.name for p in tmp_path.glob("*.jpg"))
    assert len(shots) == 8  # 3 README pages + report charts, x 2 widths
    assert "report-mobile.jpg" in shots and "home-desktop.jpg" in shots
