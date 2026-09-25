"""A refused review's card names its cause, and neither the board nor the drawer 500s.

The board labels a failed attempt through ``FailureKind(kind).label``, which raises on a
kind the enum does not register, so an unregistered ``review_unrecordable`` 500s the
whole board. The attempt is seeded from the real gate's refusal and classified by the
model's own ``save`` — it is never handed a kind.
"""

import re
from http import HTTPStatus

import pytest
from django.utils import timezone
from playwright.sync_api import Page, Response, expect
from pytest_django.live_server_helper import LiveServer

from e2e.dash.pom import BoardPage, ConsoleGuard
from teatree.core.gates.review_recordability_gate import unrecordable_review_refusal
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from tests.factories import TaskFactory, TicketFactory

_PR_REF = "souliane/teatree#4225"
_PR_URL = "https://github.com/souliane/teatree/pull/4225"
_LABEL = "Review verdict could not be recorded"
_DEFECT_TOOLTIP = "deterministic failure — re-running it hits the same wall"


@pytest.fixture
def refused_review(request: pytest.FixtureRequest) -> TaskAttempt:
    """A reviewer ticket whose PR has no recorded head, and the attempt the gate refused for it."""
    request.getfixturevalue("transactional_db")
    ticket = TicketFactory(
        issue_url=_PR_URL,
        role=Ticket.Role.REVIEWER,
        extra={},
        short_description="review a pull request with no recorded head",
    )
    task = TaskFactory(ticket=ticket, phase="reviewing")
    refusal = unrecordable_review_refusal(task, phase="reviewing")
    if refusal is None:
        msg = "the gate must refuse a reviewing task whose pull request head is not recorded"
        raise AssertionError(msg)
    attempt = TaskAttempt.objects.create(task=task, exit_code=1, ended_at=timezone.now(), error=refusal)
    task.fail(reason=refusal)
    return attempt


def _expect_ok(response: Response | None) -> None:
    if response is None or response.status != HTTPStatus.OK:
        msg = f"expected HTTP 200, got {response.status if response else 'no response'}"
        raise AssertionError(msg)


def test_a_refused_reviews_card_names_the_cause_as_a_defect(
    live_server: LiveServer, page: Page, refused_review: TaskAttempt
) -> None:
    board = BoardPage(page, live_server.url)
    _expect_ok(board.open())
    chip = board.failure_chip_on(refused_review.task.ticket_id)
    expect(chip).to_have_text(_LABEL)
    expect(chip).to_have_class(re.compile(r"\bdefect\b"))
    expect(chip).not_to_have_class(re.compile(r"\benvironmental\b"))
    expect(chip).to_have_attribute("title", _DEFECT_TOOLTIP)


def test_the_drawer_shows_the_refusal_and_nothing_500s(
    live_server: LiveServer, page: Page, console_guard: ConsoleGuard, refused_review: TaskAttempt
) -> None:
    board = BoardPage(page, live_server.url)
    _expect_ok(board.open())
    drawer = board.open_drawer_for(refused_review.task.ticket_id)
    error = drawer.attempt_errors
    expect(error).to_have_count(1)
    expect(error).to_have_text(
        re.compile(rf"^review_unrecordable: refusing to dispatch the reviewer for {re.escape(_PR_REF)}\b")
    )
    expect(error).to_contain_text("no pull request head is recorded")
    expect(error).to_have_attribute("title", f"fingerprint {refused_review.error_fingerprint}")

    # The 4s live refresh re-renders every card through the same label lookup as the first load.
    with page.expect_response(lambda response: "board/columns" in response.url) as poll:
        pass
    _expect_ok(poll.value)
    page.wait_for_load_state("networkidle")
    console_guard.raise_if_dirty()
