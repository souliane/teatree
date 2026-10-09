"""Rows intake can never see are surfaced, not left to be found by hand (#4527).

Fifty ``Ticket`` rows with no ``issue_url`` accumulated in the live control DB —
each the only surviving record of one owner request, each invisible to every
forge-scoped intake query, and none of them named by any health surface. The
check is advisory: these are rows an operator has to decide about, not an
invariant teatree broke, and a doctor FAIL nothing can clear trains its reader to
skip the whole report.

``Ticket`` carries no creation stamp, so age comes from its oldest ``Task`` — the
only timestamp the row has. A row with no task at all is reported without one
rather than hidden: it is the most provably dead shape there is.
"""

import io
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.cli.doctor.checks_dead_ticket_rows import check_dead_ticket_rows
from teatree.core.models import Session, Task, Ticket
from teatree.utils.url_slug import slack_conversation_anchor


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _lane(ticket: Ticket, *, days: int) -> Task:
    session = Session.objects.create(ticket=ticket, overlay=ticket.overlay, agent_id="answering")
    task = Task.objects.create(ticket=ticket, session=session, phase="answering", subject="answer the owner")
    Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(days=days))
    return task


def _shell(*, question: str = "", days: int = 9) -> Ticket:
    ticket = Ticket.objects.create(
        issue_url=slack_conversation_anchor(channel="D-owner", slack_ts=f"{days}.0"),
        overlay="t3-teatree",
        role=Ticket.Role.AUTHOR,
        extra={"slack_answer": {"question": question}} if question else {},
    )
    _lane(ticket, days=days)
    return ticket


class TestDeadRowsAreNamed(TestCase):
    """A row with nothing to find it by is reported with its age and its recorded text."""

    def test_a_row_intake_cannot_see_is_surfaced(self) -> None:
        _shell()

        ok, out = _echoes(check_dead_ticket_rows)

        assert ok, "the check gated the run on a decision only an operator can make"
        assert "1 ticket" in out
        assert "WARN" in out

    def test_the_recorded_owner_text_is_shown_so_it_can_be_re_filed(self) -> None:
        _shell(question="detect the open-PR bottleneck so it never recurs")

        _ok, out = _echoes(check_dead_ticket_rows)

        assert "open-PR bottleneck" in out, f"the only surviving record of the request is not shown: {out!r}"

    def test_a_healthy_row_is_not_reported(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/4527",
            overlay="t3-teatree",
            short_description="a real tracked issue",
        )
        _lane(ticket, days=9)

        _ok, out = _echoes(check_dead_ticket_rows)

        assert out == "", f"an admissible ticket was reported as dead: {out!r}"

    def test_a_row_younger_than_the_grace_window_is_left_alone(self) -> None:
        """A lane dispatched minutes ago has not failed yet — reporting it is noise."""
        _shell(days=0)

        _ok, out = _echoes(check_dead_ticket_rows)

        assert out == "", f"a freshly-dispatched lane was reported as dead: {out!r}"

    def test_a_terminal_row_is_not_reported(self) -> None:
        """A closed lane is nobody's pending request."""
        ticket = _shell()
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.MERGED)

        _ok, out = _echoes(check_dead_ticket_rows)

        assert out == "", f"a terminal row was reported as a pending dead request: {out!r}"
