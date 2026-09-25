"""RETRO_RECORDED is neither settled nor progressing — a stall there is silent (#4779).

``_SETTLED_STATES`` deliberately excludes RETRO_RECORDED (a MERGED PR is not yet
DELIVERED), but nothing else watches a ticket parked there when the retro worker
fails or never dispatches. The check is advisory, matching
``check_dead_ticket_rows``'s posture.
"""

import io
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.cli.doctor.checks_stale_retro_recorded import check_stale_retro_recorded
from teatree.core.models import Session, Task, Ticket


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _retro_recorded_ticket(*, days: int) -> Ticket:
    ticket = Ticket.objects.create(
        issue_url=f"https://github.com/souliane/teatree/issues/{1000 + days}",
        overlay="t3-teatree",
        role=Ticket.Role.AUTHOR,
        state=Ticket.State.RETRO_RECORDED,
    )
    session = Session.objects.create(ticket=ticket, overlay=ticket.overlay, agent_id="retrospecting")
    task = Task.objects.create(ticket=ticket, session=session, phase="retrospecting", subject="retrospect")
    Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(days=days))
    return ticket


class TestStaleRetroRecordedIsNamed(TestCase):
    """A ticket aged past the grace window in RETRO_RECORDED is surfaced with a remedy."""

    def test_an_aged_ticket_is_surfaced(self) -> None:
        ticket = _retro_recorded_ticket(days=9)

        ok, out = _echoes(check_stale_retro_recorded)

        assert ok, "the check gated the run on a decision only an operator can make"
        assert "WARN" in out
        assert str(ticket.pk) in out

    def test_a_ticket_younger_than_the_grace_window_is_left_alone(self) -> None:
        """A retro dispatched minutes ago has not failed yet — reporting it is noise."""
        _retro_recorded_ticket(days=0)

        _ok, out = _echoes(check_stale_retro_recorded)

        assert out == "", f"a freshly-dispatched retro was reported as stale: {out!r}"

    def test_none_aged_is_silent(self) -> None:
        """The control: with no RETRO_RECORDED ticket at all, the check says nothing."""
        Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)

        _ok, out = _echoes(check_stale_retro_recorded)

        assert out == "", f"a settled ticket was reported as a stale retro: {out!r}"

    def test_a_delivered_ticket_is_not_reported(self) -> None:
        """Once mark_delivered() fires, the ticket is off the walked state entirely."""
        ticket = _retro_recorded_ticket(days=9)
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.DELIVERED)

        _ok, out = _echoes(check_stale_retro_recorded)

        assert out == "", f"a delivered ticket was reported as stale: {out!r}"
