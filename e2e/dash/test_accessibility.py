"""Keyboard and landmark reachability across every /dash/ page.

The dash had zero ``<h1>`` and no skip link, so a screen-reader user landed in a
document whose outline started at ``<h2>`` and a keyboard user had to tab the whole
header before reaching content. The Django-level tests assert the MARKUP; these
assert the two behaviours only a browser can show — that the skip link is off-screen
until focused, and that activating it reaches the main region.
"""

from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

_PAGES = ("board", "live", "health", "loops", "presets", "settings")


def test_every_page_exposes_exactly_one_level_one_heading(live_server: LiveServer, page: Page) -> None:
    for name in _PAGES:
        page.goto(f"{live_server.url}/dash/{name}/")
        expect(page.get_by_role("heading", level=1)).to_have_count(1)


def test_the_skip_link_is_offscreen_until_focused_then_reaches_main(live_server: LiveServer, page: Page) -> None:
    """Off-screen is a POSITION, not Playwright's notion of hidden.

    An absolutely-positioned element parked at ``left: -9999px`` still has a non-empty
    box and no ``display:none`` / ``visibility:hidden``, so Playwright reports it
    VISIBLE. Assert the position the pattern actually relies on.
    """
    page.goto(f"{live_server.url}/dash/board/")
    skip = page.get_by_role("link", name="Skip to main content")

    expect(skip).to_have_css("left", "-9999px")

    page.keyboard.press("Tab")
    expect(skip).to_be_focused()
    expect(skip).not_to_have_css("left", "-9999px")

    skip.press("Enter")
    expect(page.locator("#dash-main")).to_be_visible()


def test_the_navigation_is_a_named_landmark(live_server: LiveServer, page: Page) -> None:
    page.goto(f"{live_server.url}/dash/board/")
    expect(page.get_by_role("navigation", name="Dashboard sections")).to_be_visible()
