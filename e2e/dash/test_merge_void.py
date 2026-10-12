"""A voided merge leaves the Landed column, and the drawer shows the void without losing the merge history.

The rows are written by the real path: ``reconcile_merged`` on a keystone audit, then
``plan_void`` + ``apply_void`` as a human answering yes at a terminal (forge stubbed).
"""

import pytest
from playwright.sync_api import Locator, Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage
from teatree.core.models.ticket import Ticket
from tests.void_merge_fixtures import landed_ticket, void_pr

State = Ticket.State
CHIP = "example-org/example-repo!41 · Merge voided"


@pytest.fixture
def voided_ticket(request: pytest.FixtureRequest) -> Ticket:
    request.getfixturevalue("transactional_db")
    ticket = landed_ticket()
    void_pr()
    ticket.refresh_from_db()
    return ticket


def _cells(row: Locator) -> Locator:
    return row.locator("td")


def _history(page: Page) -> Locator:
    return page.locator("#drawer h3:has-text('Transition history') + table tbody tr")


def test_a_voided_ticket_leaves_the_landed_column(live_server: LiveServer, page: Page, voided_ticket: Ticket) -> None:
    board = BoardPage(page, live_server.url)
    board.open()

    card = f'.card[data-ticket="{voided_ticket.pk}"]'
    building = page.locator('.kanban-group[data-group="building"]')
    expect(building.locator(f'.kanban-column[data-state="{State.CODED}"] {card}')).to_be_visible()
    expect(page.locator(f'.kanban-group[data-group="landed"] {card}')).to_have_count(0)
    expect(board.card_by_id(voided_ticket.pk).locator(".chip.pr")).to_have_text(CHIP)


def test_the_drawer_shows_the_void_and_keeps_the_merge_history(
    live_server: LiveServer, page: Page, voided_ticket: Ticket
) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    drawer = board.open_drawer_for(voided_ticket.pk)

    expect(_cells(_history(page).last).nth(1)).to_have_text(State.MERGED)
    expect(_cells(_history(page).last).nth(2)).to_have_text(State.CODED)
    expect(_cells(_history(page).last).nth(3)).to_have_text("void_merge")
    merge_entry = _history(page).filter(has_text="reconcile_merged")
    expect(merge_entry).to_have_count(1)
    expect(_cells(merge_entry).nth(1)).to_have_text(State.CODED)
    expect(_cells(merge_entry).nth(2)).to_have_text(State.MERGED)
    expect(drawer.root.locator(".chip.pr")).to_have_text(CHIP)
    expect(drawer.root.get_by_role("button", name="void_merge", exact=True)).to_have_count(0)
