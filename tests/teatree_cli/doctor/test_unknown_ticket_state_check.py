"""A Ticket value outside ``Ticket.State.values`` is orphaned and must be named (#4779).

The rename ships with no dual-read shim, so a row that pre-rename code writes after
migration 0093 ran — ``state``, or the ``ignored_from`` / ``reopened_from`` snapshot
``unignore()`` assigns straight back into ``state`` — matches no transition source and
no board column. Nothing else reads such a row as wrong, so the doctor check does.
"""

import io
from collections.abc import Callable
from contextlib import redirect_stdout

from django.test import TestCase

from teatree.cli.doctor.checks_ticket_state_values import check_unknown_ticket_states
from teatree.core.models import Ticket, TicketTransition


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _ticket(n: int, **fields: object) -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", issue_url=f"https://example.com/issues/{n}", **fields)


class TestUnknownTicketStatesAreFailed(TestCase):
    def test_a_retired_state_value_fails_the_run_and_names_the_ticket(self) -> None:
        ticket = _ticket(1)
        Ticket.objects.filter(pk=ticket.pk).update(state="in_review")

        ok, out = _echoes(check_unknown_ticket_states)

        assert not ok
        assert "FAIL" in out
        assert f"{ticket.pk} state='in_review'" in out

    def test_a_retired_snapshot_in_extra_fails_the_run(self) -> None:
        ignored = _ticket(2, state=Ticket.State.IGNORED, extra={"ignored_from": "reviewed"})
        reopened = _ticket(3, extra={"reopened_from": "shipped", "note": "kept"})

        ok, out = _echoes(check_unknown_ticket_states)

        assert not ok
        assert f"{ignored.pk} ignored_from='reviewed'" in out
        assert f"{reopened.pk} reopened_from='shipped'" in out

    def test_an_unhashable_snapshot_is_flagged_as_a_bad_row_not_an_unreadable_table(self) -> None:
        listed = _ticket(4, state=Ticket.State.IGNORED, extra={"ignored_from": ["reviewed"]})
        mapped = _ticket(5, extra={"reopened_from": {"state": "shipped"}})

        ok, out = _echoes(check_unknown_ticket_states)

        assert not ok
        assert "UNVERIFIED" not in out
        assert f"{listed.pk} ignored_from=['reviewed']" in out
        assert f"{mapped.pk} reopened_from={{'state': 'shipped'}}" in out

    def test_a_retired_transition_endpoint_fails_the_run(self) -> None:
        ticket = _ticket(6)
        edge = TicketTransition.objects.create(ticket=ticket, from_state="scoped", to_state="started")
        back = TicketTransition.objects.create(ticket=ticket, from_state="in_review", to_state="merged")

        ok, out = _echoes(check_unknown_ticket_states)

        assert not ok
        assert f"transition {edge.pk} to_state='started'" in out
        assert f"transition {back.pk} from_state='in_review'" in out

    def test_only_known_values_is_silent(self) -> None:
        """The control: every live value, including a known snapshot, raises nothing."""
        for n, state in enumerate(Ticket.State.values):
            _ticket(10 + n, state=state)
        _ticket(99, state=Ticket.State.IGNORED, extra={"ignored_from": Ticket.State.SELF_REVIEWED})
        TicketTransition.objects.create(
            ticket=_ticket(100), from_state=Ticket.State.SCOPED, to_state=Ticket.State.WORK_STARTED
        )

        ok, out = _echoes(check_unknown_ticket_states)

        assert ok
        assert out == "", f"a known state was reported as unknown: {out!r}"
