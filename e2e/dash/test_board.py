"""Board flow order, ticket placement, filtering, and poll-swap stability.

The signature regression: a poll tick morphs the board in place without
resetting horizontal scroll or dropping the open side panel (#3162).
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage, SeededBoard
from teatree.core.models.ticket import Ticket

State = Ticket.State

_SCROLL_TARGET = 240


@pytest.mark.usefixtures("seeded_board")
def test_rail_shows_the_four_groups_in_flow_order(live_server: LiveServer, page: Page) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    expect(board.rail_nodes).to_have_text(["Backlog", "Building", "Reviewing", "Landed"])


def test_seeded_tickets_land_in_their_state_columns(
    live_server: LiveServer, page: Page, seeded_board: SeededBoard
) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    expect(board.card_in_column(State.NOT_STARTED, seeded_board.backlog.pk)).to_be_visible()
    expect(board.card_in_column(State.STARTED, seeded_board.building.pk)).to_be_visible()
    expect(board.card_in_column(State.IN_REVIEW, seeded_board.reviewing.pk)).to_be_visible()
    expect(board.card_in_column(State.MERGED, seeded_board.landed.pk)).to_be_visible()


def test_text_filter_narrows_the_cards(live_server: LiveServer, page: Page, seeded_board: SeededBoard) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    page.get_by_placeholder("search #/description").fill("widget")
    page.get_by_role("button", name="Filter").click()
    expect(board.card_by_id(seeded_board.building.pk)).to_be_visible()
    expect(board.card_by_id(seeded_board.backlog.pk)).to_have_count(0)


def test_poll_preserves_board_scroll_and_open_drawer(
    live_server: LiveServer, page: Page, seeded_board: SeededBoard
) -> None:
    page.set_viewport_size({"width": 700, "height": 800})
    board = BoardPage(page, live_server.url)
    board.open()
    # Open the side panel, then scroll the (wide, 13-column) board horizontally.
    board.open_drawer_for(seeded_board.reviewing.pk)
    expect(page.locator("#drawer .drawer")).to_be_visible()
    page.eval_on_selector(".kanban-groups", f"el => el.scrollLeft = {_SCROLL_TARGET}")

    # Await one real poll tick, then assert the morph preserved scroll + the panel.
    with page.expect_response(lambda r: "board/columns" in r.url):
        pass
    page.wait_for_timeout(300)
    scroll = page.eval_on_selector(".kanban-groups", "el => el.scrollLeft")
    if scroll != _SCROLL_TARGET:
        msg = f"board scroll reset to {scroll}, expected {_SCROLL_TARGET} (morph must preserve it)"
        raise AssertionError(msg)
    expect(page.locator("#drawer .drawer")).to_be_visible()
