"""``ReproWaiver`` — the human-authorized escape for a repro-less FIX (#118).

The waiver is deliberately HUMAN-authorized (maker != checker): the executing
agent can never waive the very repro discipline that exists because its
self-judgment is unreliable. The guarded ``record`` factory refuses a
maker/coding-agent/loop approver and an empty reason.
"""

import pytest
from django.test import TestCase

from teatree.core.models import ReproWaiver, ReproWaiverError, Ticket


def _fix_ticket() -> Ticket:
    return Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FIX)


class TestReproWaiverRecord(TestCase):
    def test_human_approver_records_a_waiver(self) -> None:
        ticket = _fix_ticket()
        waiver = ReproWaiver.record(ticket=ticket, approver_id="souliane", reason="hardware-timing race")
        assert waiver.approver_id == "souliane"
        assert ReproWaiver.objects.filter(ticket=ticket).exists()

    def test_maker_approver_is_refused(self) -> None:
        # RED-7: a self-authored waiver (maker role) must be refused.
        with pytest.raises(ReproWaiverError) as exc:
            ReproWaiver.record(ticket=_fix_ticket(), approver_id="maker", reason="race")
        assert "maker" in str(exc.value)

    def test_coding_agent_approver_is_refused(self) -> None:
        with pytest.raises(ReproWaiverError):
            ReproWaiver.record(ticket=_fix_ticket(), approver_id="coding-agent", reason="race")

    def test_loop_approver_is_refused(self) -> None:
        with pytest.raises(ReproWaiverError):
            ReproWaiver.record(ticket=_fix_ticket(), approver_id="merge-loop", reason="race")

    def test_empty_reason_is_refused(self) -> None:
        with pytest.raises(ReproWaiverError) as exc:
            ReproWaiver.record(ticket=_fix_ticket(), approver_id="souliane", reason="   ")
        assert "reason" in str(exc.value)

    def test_empty_approver_is_refused(self) -> None:
        with pytest.raises(ReproWaiverError):
            ReproWaiver.record(ticket=_fix_ticket(), approver_id="  ", reason="race")

    def test_str_renders_ticket_and_approver(self) -> None:
        waiver = ReproWaiver.record(ticket=_fix_ticket(), approver_id="souliane", reason="race")
        assert "by souliane" in str(waiver)
