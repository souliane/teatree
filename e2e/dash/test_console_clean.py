"""Every dash page + the admin index load with no console errors and no 404s (#3162).

Fonts included: a missing vendored woff2 or CSS/JS returns a 404 that the console
guard's response listener catches, so a broken static wiring reds this spec.
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import ConsoleGuard

_DASH_PATHS = ("/dash/board/", "/dash/health/", "/dash/loops/")


@pytest.mark.usefixtures("seeded_board")
@pytest.mark.parametrize("path", _DASH_PATHS)
def test_dash_page_loads_clean(live_server: LiveServer, page: Page, console_guard: ConsoleGuard, path: str) -> None:
    page.goto(f"{live_server.url}{path}")
    expect(page.locator("header.dash-header")).to_be_visible()
    page.wait_for_load_state("networkidle")
    console_guard.raise_if_dirty()


@pytest.mark.usefixtures("seeded_board")
def test_admin_index_loads_clean(live_server: LiveServer, page: Page, console_guard: ConsoleGuard) -> None:
    page.goto(f"{live_server.url}/admin/")
    expect(page.locator("#site-name")).to_be_visible()
    # A browser fires its unprompted /favicon.ico request AFTER first paint, so the
    # first visible element resolves well before it lands. Reading the guard there
    # made the assertion a coin flip — the missing favicon reddened this spec on a
    # real chromium and passed on the same binary moments later. Settling the
    # network first catches a late 404 deterministically rather than sometimes.
    page.wait_for_load_state("networkidle")
    console_guard.raise_if_dirty()
