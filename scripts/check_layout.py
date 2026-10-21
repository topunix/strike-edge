"""Playwright layout check: every page at 375px and 1280px, no horizontal overflow,
charts render, screenshots saved.

Usage: python scripts/check_layout.py BASE_URL OUT_DIR
Exit code 1 if any check fails. Used by tests/test_browser.py and for README screenshots.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Browser, Page, sync_playwright

PAGES = {
    "home": "/",
    "home-all": "/?tab=all",
    "home-hold": "/?tab=hold&side=long",
    "home-only": "/?tab=only",
    "report": "/report",
}
SHOT_PAGES = {"home", "home-hold", "report"}  # saved for the README; all pages are checked
WIDTHS = {"mobile": (375, 812), "desktop": (1280, 900)}


def launch(p) -> Browser:
    try:
        return p.chromium.launch()
    except Exception:
        return p.chromium.launch(executable_path="/opt/pw-browsers/chromium")


def overflow(page: Page) -> dict:
    return page.evaluate(
        """() => {
          const de = document.documentElement;
          const wide = [];
          document.querySelectorAll('body *').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.right > de.clientWidth + 1) {
              wide.push(el.tagName + '.' + (el.className || '') + ' right=' + Math.round(r.right));
            }
          });
          return { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth, wide: wide.slice(0, 8) };
        }"""
    )


def small_targets(page: Page) -> list[str]:
    return page.evaluate(
        """() => {
          const bad = [];
          document.querySelectorAll('a.btn, button, input[type=number], select, .seg a, .tabs a').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0 && (r.height < 43.5 || r.width < 43.5)) {
              bad.push((el.tagName + ' ' + (el.textContent || el.name || '').trim().slice(0, 24)) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
            }
          });
          return bad.slice(0, 8);
        }"""
    )


def run(base: str, out: Path, expect_charts: bool = True) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = launch(p)
        for wname, (w, h) in WIDTHS.items():
            ctx = browser.new_context(
                viewport={"width": w, "height": h}, device_scale_factor=2 if w < 500 else 1
            )
            for pname, path in PAGES.items():
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                page.goto(base + path, wait_until="load")
                if path == "/report" and expect_charts:
                    try:
                        page.wait_for_function(
                            "document.getElementById('chart-funding').dataset.rendered === '1' "
                            "&& document.getElementById('chart-cum').dataset.rendered === '1'",
                            timeout=15000,
                        )
                    except Exception:
                        problems.append(f"{pname}@{w}: charts did not render")
                    page.wait_for_timeout(300)
                o = overflow(page)
                if o["scrollWidth"] > o["clientWidth"] + 1 or o["wide"]:
                    problems.append(f"{pname}@{w}: horizontal overflow {o}")
                if w < 500:
                    t = small_targets(page)
                    if t:
                        problems.append(f"{pname}@{w}: tap targets under 44px {t}")
                if errors:
                    problems.append(f"{pname}@{w}: JS errors {errors}")
                if path == "/report" and expect_charts:
                    for cid in ("chart-funding", "chart-cum"):
                        box = page.locator("#" + cid).bounding_box()
                        if not box or box["width"] < 200 or box["height"] < 150:
                            problems.append(f"{pname}@{w}: {cid} too small {box}")
                if pname in SHOT_PAGES:
                    page.screenshot(
                        path=str(out / f"{pname}-{wname}.jpg"),
                        type="jpeg",
                        quality=70,
                        full_page=(pname == "home" or (pname == "report" and w >= 500)),
                    )
                if path == "/report" and expect_charts:
                    page.locator(".charts").screenshot(
                        path=str(out / f"report-charts-{wname}.jpg"), type="jpeg", quality=70
                    )
                page.close()
            ctx.close()
        browser.close()
    return problems


if __name__ == "__main__":
    probs = run(sys.argv[1], Path(sys.argv[2]))
    print("\n".join(probs) if probs else "layout checks passed")
    sys.exit(1 if probs else 0)
