"""The theme toggle switches and persists; first load follows the OS scheme.

The header toggle flips data-theme and persists it across a reload; with no pin
a fresh load respects prefers-color-scheme (#3162).
"""

from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage

_DARK_BG = "rgb(16, 20, 15)"  # --bg #10140f (steeped)
_LIGHT_BG = "rgb(246, 247, 244)"  # --bg #f6f7f4 (porcelain)


def test_toggle_switches_theme_and_persists_across_reload(live_server: LiveServer, page: Page) -> None:
    # Emulate dark + no pin, so the first toggle is deterministic: dark -> light.
    page.emulate_media(color_scheme="dark")
    BoardPage(page, live_server.url).open()
    html = page.locator("html")
    toggle = page.locator("[data-theme-toggle]")

    toggle.click()
    expect(html).to_have_attribute("data-theme", "light")
    toggle.click()
    expect(html).to_have_attribute("data-theme", "dark")

    page.reload()
    expect(html).to_have_attribute("data-theme", "dark")  # persisted via localStorage


def test_first_load_respects_prefers_color_scheme(live_server: LiveServer, page: Page) -> None:
    page.emulate_media(color_scheme="dark")
    page.goto(f"{live_server.url}/dash/board/")
    expect(page.locator("body")).to_have_css("background-color", _DARK_BG)

    page.emulate_media(color_scheme="light")
    page.reload()
    expect(page.locator("body")).to_have_css("background-color", _LIGHT_BG)
