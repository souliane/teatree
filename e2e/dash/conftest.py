"""Fixtures for the behavioral /dash/ e2e lane (pytest-playwright + live_server).

This lane lives OUTSIDE ``testpaths=[tests]``, so the default ``uv run pytest`` never
collects it and it never touches the 93% coverage floor. A dedicated CI job installs
chromium and runs ``uv run pytest e2e/dash --ds=e2e.dash.settings``. Assertions are
behavioral (htmx swap stability, drawer flows, mermaid render, loop round-trips,
theme toggle) — never pixel snapshots (the repo's recorded stance); screenshots are
run evidence only.
"""

import os

# pytest-playwright's sync API runs an event loop in the test thread, which trips
# Django's async-unsafe guard on the (thread-safe, in-memory) test-DB setup. This is
# the documented escape for the sync-Playwright + sync-Django combination; set before
# any Django DB call (conftest imports before fixtures run).
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from playwright.sync_api import Page

from e2e.dash.pom import ConsoleGuard, SeededBoard
from teatree.core.models.loop import Loop
from teatree.core.models.ticket import Ticket
from tests.factories import PullRequestFactory, TicketFactory, TicketTransitionFactory

State = Ticket.State


@pytest.fixture
def seeded_board(request: pytest.FixtureRequest) -> SeededBoard:
    """One ticket in each of the four FSM groups + a transition, a PR and a loop.

    Depends on ``transactional_db`` (not ``db``) so the rows are committed and
    visible to the ``live_server`` thread that serves the browser.
    """
    request.getfixturevalue("transactional_db")
    board = SeededBoard(
        backlog=TicketFactory(state=State.NOT_STARTED, short_description="triage the inbox"),
        building=TicketFactory(state=State.STARTED, short_description="build the widget"),
        reviewing=TicketFactory(state=State.IN_REVIEW, short_description="awaiting cold review"),
        landed=TicketFactory(state=State.MERGED, short_description="landed the change"),
    )
    TicketTransitionFactory(ticket=board.reviewing, from_state=State.STARTED, to_state=State.CODED)
    PullRequestFactory(ticket=board.reviewing)
    Loop.objects.create(name="e2e_loop", script="teatree.loops.review", delay_seconds=60)
    return board


@pytest.fixture
def console_guard(page: Page) -> ConsoleGuard:
    """Records console errors, failed requests + 4xx/5xx responses for clean-load asserts."""
    guard = ConsoleGuard()
    page.on("console", guard.on_console)
    page.on("pageerror", guard.on_page_error)
    page.on("requestfailed", guard.on_request_failed)
    page.on("response", guard.on_response)
    return guard
