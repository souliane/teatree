"""The always-visible loopback terminal button (top-level gap fix, #3162).

The ttyd "Debug session" button used to live only inside the per-ticket drawer, so
from the main dashboard there was no way to open a terminal. This header button
opens a fresh loopback terminal from every page; the per-ticket drawer button stays.
"""

import re
import shutil

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage

_TERMINAL = "Open a loopback terminal session"
_LOOPBACK_URL = re.compile(r"^http://127\.0\.0\.1:\d+")

# The click spec drives the REAL launcher, which resolves ``shutil.which("ttyd")``
# and renders the install hint instead of a launch URL when it is absent. That is a
# HOST dependency, not a code path worth mocking away — the point of the spec is
# that the button spawns a real terminal.
#
# The dependency is DECLARED where each environment that must run this provisions
# it: ``.github/workflows/ci.yml`` apt-installs ttyd for the e2e-dash job (so the
# authoritative lane never skips and the coverage is not quietly dead), and
# ``deploy/Dockerfile`` installs it into the image the dashboard actually serves
# from. A developer host with neither gets a VISIBLE skip naming the install
# command instead of a red that reads like a product bug. Conditional by
# construction, so it is not the dead coverage ``check_no_silent_skip`` bans.
requires_ttyd = pytest.mark.skipif(
    shutil.which("ttyd") is None,
    reason="ttyd is not on PATH — install it (apt install ttyd / brew install ttyd); "
    "CI and the deploy image both ship it",
)


@pytest.mark.usefixtures("seeded_board")
def test_terminal_button_visible_on_board(live_server: LiveServer, page: Page) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    # Top-level — no drawer opened. This is the gap the button closes.
    expect(page.locator("#drawer")).to_be_empty()
    expect(page.get_by_role("button", name=_TERMINAL)).to_be_visible()


@requires_ttyd
@pytest.mark.usefixtures("seeded_board", "accept_dialogs")
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
