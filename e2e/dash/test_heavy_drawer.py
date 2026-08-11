"""A long-lived ticket's card still opens its drawer, and Sessions is reachable (#3873).

The existing drawer specs seed four tiny tickets and click inside the first poll
window, which is exactly why they stayed green while the deployed board's card 287
did nothing on click: its drawer was 5,005,822 bytes of provenance rows, and the
board's own 4s poll was re-morphing the card mid-click. This spec seeds a ticket
whose history crosses the drawer caps and clicks it in a real browser.
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.dash.ticket_detail import ATTEMPT_ROWS, TASK_ROWS, TRANSITION_ROWS
from tests.factories import TaskFactory, TicketTransitionFactory

State = Ticket.State

_EXTRA = 20


@pytest.fixture
def heavy_ticket(request: pytest.FixtureRequest) -> Ticket:
    """One ticket carrying more history than every drawer cap admits."""
    request.getfixturevalue("transactional_db")
    ticket = Ticket.objects.create(state=State.STARTED, short_description="worked for weeks")
    for _ in range(TRANSITION_ROWS + _EXTRA):
        TicketTransitionFactory(ticket=ticket, from_state=State.SCOPED, to_state=State.STARTED)
    for _ in range(TASK_ROWS + _EXTRA):
        task = TaskFactory(ticket=ticket, phase="coding")
        TaskAttempt.objects.bulk_create(
            TaskAttempt(
                task=task,
                model="claude-opus-4-8",
                error="a recorded failure reason that occupies a realistic amount of the row",
            )
            for _ in range(ATTEMPT_ROWS + _EXTRA)
        )
    return ticket


def test_a_long_lived_cards_drawer_still_opens(live_server: LiveServer, page: Page, heavy_ticket: Ticket) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    drawer = board.open_drawer_for(heavy_ticket.pk)
    expect(drawer.root.locator(".drawer")).to_be_visible()
    expect(drawer.root).to_contain_text("worked for weeks")


def test_the_drawer_states_what_it_truncated(live_server: LiveServer, page: Page, heavy_ticket: Ticket) -> None:
    board = BoardPage(page, live_server.url)
    board.open()
    drawer = board.open_drawer_for(heavy_ticket.pk)
    expect(drawer.root).to_contain_text(f"of {TRANSITION_ROWS + _EXTRA}")
    expect(drawer.root).to_contain_text(f"of {TASK_ROWS + _EXTRA}")


def test_a_card_survives_the_boards_own_poll(live_server: LiveServer, page: Page, heavy_ticket: Ticket) -> None:
    """The click must land on the card even after several poll cycles have morphed it.

    The board polls every 4s over itself; before ``hx-sync`` and the per-card ``id``
    a poll could still be in flight when the next fired, and the click landed on an
    element being replaced.
    """
    board = BoardPage(page, live_server.url)
    board.open()
    page.wait_for_timeout(9_000)
    drawer = board.open_drawer_for(heavy_ticket.pk)
    expect(drawer.root.locator(".drawer")).to_be_visible()


def test_sessions_is_reachable_from_the_nav_and_lists_transcripts(
    live_server: LiveServer, page: Page, heavy_ticket: Ticket
) -> None:
    TaskAttempt.objects.filter(task__ticket=heavy_ticket).update(agent_session_id="e2e-session")
    board = BoardPage(page, live_server.url)
    board.open()
    page.get_by_role("navigation", name="Dashboard sections").get_by_role("link", name="Sessions").click()
    expect(page.get_by_role("heading", name="Agent sessions")).to_be_visible()
    page.get_by_role("link", name="e2e-session").first.click()
    expect(page.get_by_role("heading", name="Agent transcript")).to_be_visible()
