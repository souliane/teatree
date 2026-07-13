"""The right side panel: open, legal transitions, round trip, and dismissal.

Card-click opens it, only legal FSM transitions render, executing one moves the
card, history rows show, and Esc / the close button dismiss it (#3162).
"""

from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage, SeededBoard
from teatree.core.models.ticket import Ticket

State = Ticket.State


def test_card_click_opens_the_side_panel(live_server: LiveServer, page: Page, seeded_board: SeededBoard) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    drawer = board.open_drawer_for(seeded_board.building.pk)
    expect(drawer.root.locator(".drawer")).to_be_visible()
    expect(drawer.root).to_contain_text("build the widget")


def test_only_legal_transitions_render(live_server: LiveServer, page: Page, seeded_board: SeededBoard) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    drawer = board.open_drawer_for(seeded_board.backlog.pk)
    # `scope` is a legal move from NOT_STARTED; `deliver`/`ship` belong to far later
    # states and must never be offered here (exact-name match — `merge` would collide
    # with the legal `reconcile_merged`).
    expect(drawer.transition_buttons.filter(has_text="scope")).to_have_count(1)
    expect(drawer.root.get_by_role("button", name="deliver", exact=True)).to_have_count(0)
    expect(drawer.root.get_by_role("button", name="ship", exact=True)).to_have_count(0)


def test_executing_a_transition_moves_the_card(live_server: LiveServer, page: Page, seeded_board: SeededBoard) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    drawer = board.open_drawer_for(seeded_board.backlog.pk)
    drawer.transition_buttons.filter(has_text="scope").click()
    # The POST redirects to the board; the card is now in the SCOPED column.
    expect(board.card_in_column(State.SCOPED, seeded_board.backlog.pk)).to_be_visible()
    expect(board.card_in_column(State.NOT_STARTED, seeded_board.backlog.pk)).to_have_count(0)


def test_transition_history_rows_render(live_server: LiveServer, page: Page, seeded_board: SeededBoard) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    drawer = board.open_drawer_for(seeded_board.reviewing.pk)
    # The seeded STARTED -> CODED transition shows as a history row with hued chips.
    expect(drawer.root.get_by_role("heading", name="Transition history")).to_be_visible()
    expect(drawer.history_rows.filter(has=page.locator(".chip.state"))).not_to_have_count(0)


def test_escape_closes_the_panel(live_server: LiveServer, page: Page, seeded_board: SeededBoard) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    drawer = board.open_drawer_for(seeded_board.building.pk)
    expect(drawer.root.locator(".drawer")).to_be_visible()
    page.keyboard.press("Escape")
    expect(drawer.root).to_be_empty()


def test_close_button_closes_the_panel(live_server: LiveServer, page: Page, seeded_board: SeededBoard) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    drawer = board.open_drawer_for(seeded_board.building.pk)
    drawer.close_button.click()
    expect(drawer.root).to_be_empty()
