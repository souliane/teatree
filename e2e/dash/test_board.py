"""Board flow order, ticket placement, filtering, and poll-swap stability.

The signature regression: a poll tick morphs the board in place without
resetting horizontal scroll or dropping the open side panel (#3162).
"""

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage, PrBoardPage, SeededBoard
from teatree.core.models.ticket import Ticket
from tests.factories import TicketFactory

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
    backlog_card = board.card_in_column(State.NOT_STARTED, seeded_board.backlog.pk)
    expect(backlog_card).to_be_hidden()
    board.expand_column(State.NOT_STARTED)
    expect(backlog_card).to_be_visible()
    expect(board.card_in_column(State.WORK_STARTED, seeded_board.building.pk)).to_be_visible()
    expect(board.card_in_column(State.REVIEW_REQUESTED, seeded_board.reviewing.pk)).to_be_visible()
    expect(board.card_in_column(State.MERGED, seeded_board.landed.pk)).to_be_visible()


def test_default_board_groups_the_open_pr_by_who_acts_next(
    live_server: LiveServer, page: Page, seeded_board: SeededBoard
) -> None:
    pr = seeded_board.reviewing.pull_requests.get()
    board = PrBoardPage(page, live_server.url)
    board.open()
    expect(page.locator("[data-pr-group] h2")).to_have_text(
        [
            re.compile(name)
            for name in ("Needs you", "Waiting on colleagues", "Factory working", "Ready to merge", "Blocked")
        ]
    )
    card = board.group("Blocked").locator(board.card(f"{pr.repo}!{pr.iid}"))
    expect(card).to_contain_text("PR state unreadable")
    expect(card).to_contain_text("Approvals")
    expect(card).to_contain_text("Last activity")


def test_an_expanded_collapsed_column_stays_open_across_polls(
    live_server: LiveServer, page: Page, seeded_board: SeededBoard
) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    backlog = page.locator(f'details[data-state="{State.NOT_STARTED}"]')
    expect(backlog).not_to_have_attribute("open", "")
    board.expand_column(State.NOT_STARTED)
    expect(backlog).to_have_attribute("open", "")
    expect(board.card_by_id(seeded_board.backlog.pk)).to_be_visible()

    with page.expect_response(lambda r: "board/columns" in r.url):
        pass
    page.wait_for_timeout(300)
    expect(backlog).to_have_attribute("open", "")


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


def test_review_now_on_a_card_enqueues_without_opening_the_drawer_first(
    live_server: LiveServer, page: Page, seeded_board: SeededBoard
) -> None:
    """#4085: prioritise a PR from the row it is already on, not a raw DB query plus a CLI call."""
    testable = TicketFactory(state=State.TESTED, short_description="ready for review")
    board = BoardPage(page, live_server.url)
    board.open()
    expect(board.enqueue_buttons_on(seeded_board.reviewing.pk)).to_have_count(0)
    review_now = board.enqueue_buttons_on(testable.pk).filter(has_text="Review now")
    expect(review_now).to_be_enabled()

    page.on("dialog", lambda dialog: dialog.accept())
    review_now.click()

    # The drawer swaps in carrying the queued task — the card press is its own feedback.
    expect(page.locator("#drawer .drawer")).to_be_visible()
    expect(page.locator("#drawer")).to_contain_text("reviewing")
