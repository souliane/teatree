"""The drawer's mermaid lifecycle diagram actually renders to SVG, error-free (#3162).

This is the coverage the test client cannot reach — the pre-redesign vendored bundle
never even defined the mermaid global, so the diagram never drew. The theme comes
from the CSS tokens (theme: base), so it renders in light and dark alike.
"""

from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage, ConsoleGuard, SeededBoard


def test_mermaid_svg_renders_in_the_drawer(
    live_server: LiveServer, page: Page, console_guard: ConsoleGuard, seeded_board: SeededBoard
) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    drawer = board.open_drawer_for(seeded_board.reviewing.pk)
    expect(drawer.mermaid_svg).to_be_visible()
    console_guard.raise_if_dirty()
