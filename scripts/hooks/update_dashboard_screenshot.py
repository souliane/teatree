#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["playwright>=1.52"]
# requires-python = ">=3.12"
# ///
"""Regenerate docs/dashboard-screenshot.png from the golden HTML test fixture.

Runs as a pre-commit hook when the golden HTML or dashboard renderer changes.
Requires Chromium for Playwright — install once with: uv run playwright install chromium
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
GOLDEN_HTML = ROOT_DIR / "tests" / "assets" / "golden_dashboard.html"
SCREENSHOT_PATH = ROOT_DIR / "docs" / "dashboard-screenshot.png"

# Dashboard is designed for ~1400px max-width
VIEWPORT_WIDTH = 1400
# Generous height — we capture full page anyway
VIEWPORT_HEIGHT = 900


def main() -> int:
    if not GOLDEN_HTML.exists():
        print(f"Error: {GOLDEN_HTML} not found", file=sys.stderr)
        return 1

    old_bytes = SCREENSHOT_PATH.read_bytes() if SCREENSHOT_PATH.exists() else b""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            device_scale_factor=2,
        )
        page.goto(GOLDEN_HTML.as_uri())
        page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
        browser.close()

    if SCREENSHOT_PATH.read_bytes() == old_bytes:
        return 0

    print(f"Updated {SCREENSHOT_PATH.relative_to(ROOT_DIR)}")
    return 1  # signal pre-commit that file was modified


if __name__ == "__main__":
    sys.exit(main())
