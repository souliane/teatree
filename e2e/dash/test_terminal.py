"""The always-visible loopback terminal button (top-level gap fix, #3162).

The ttyd "Debug session" button used to live only inside the per-ticket drawer, so
from the main dashboard there was no way to open a terminal. This header button
opens a fresh loopback terminal from every page; the per-ticket drawer button stays.
"""

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage

_TERMINAL = "Open a loopback terminal session"
_LOOPBACK_URL = re.compile(r"^http://127\.0\.0\.1:\d+")


@pytest.mark.usefixtures("seeded_board")
def test_terminal_button_visible_on_board(live_server: LiveServer, page: Page) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    # Top-level — no drawer opened. This is the gap the button closes.
    expect(page.locator("#drawer")).to_be_empty()
    expect(page.get_by_role("button", name=_TERMINAL)).to_be_visible()


@pytest.mark.usefixtures("seeded_board")
def test_terminal_button_click_renders_launch_url(live_server: LiveServer, page: Page) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    page.get_by_role("button", name=_TERMINAL).click()
    result = page.locator("#terminal-result")
    # The result auto-opens a new tab; the link + data-ttyd-launch attr are the
    # popup-blocked fallback / auto-open source (directive #3).
    expect(result).to_contain_text("Opening terminal in a new tab")
    expect(result.locator("[data-ttyd-launch]")).to_have_attribute("data-ttyd-launch", _LOOPBACK_URL)
    expect(result.get_by_role("link")).to_have_attribute("href", _LOOPBACK_URL)
