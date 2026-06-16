"""The rubric-gate honesty-escalation backstop + the mark_cleared landing (#2263).

Two deterministic seams that bracket trigger #4 (shipped a job not verified
complete):

- RAISE backstop — ``rubric_gate.check_rubric_satisfied`` records a
    ``shipped_incomplete`` :class:`HonestyEscalation` for the ticket's active
    session before raising the refusal, so the next verification spawn routes
    to the most-honest model.
- CLEAR landing — ``ticket rubric-grade`` clears the ticket's active
    escalations when it records a fully-passed rubric (the honest,
    verified-complete outcome ends the escalation).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates.rubric_gate import RubricNotSatisfiedError, check_rubric_satisfied
from teatree.core.models import HonestyEscalation, Rubric, Session, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_GRADER = "cold-reviewer"
_AGENT = "33333333-4444-5555-6666-777777777777"


@contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch(
        "teatree.core.gates.rubric_gate.get_effective_settings",
        return_value=UserSettings(require_rubric_verification=required),
    ):
        yield


class TestRubricRefusalBackstop(TestCase):
    """The #4 backstop: a rubric-gate refusal writes the ``shipped_incomplete`` row."""

    def _ticket_with_session(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        Session.objects.create(overlay="t3-teatree", ticket=ticket, agent_id=_AGENT)
        return ticket

    def test_refusal_records_shipped_incomplete_escalation(self) -> None:
        ticket = self._ticket_with_session()
        with _gate(required=True), pytest.raises(RubricNotSatisfiedError):
            check_rubric_satisfied(ticket, _SHA, transition="merge")
        row = HonestyEscalation.objects.get(session_id=_AGENT)
        assert row.reason == HonestyEscalation.Reason.SHIPPED_INCOMPLETE
        assert HonestyEscalation.is_active(_AGENT) is True

    def test_no_refusal_no_escalation(self) -> None:
        # A fully-passed rubric does not refuse → no escalation is written.
        ticket = self._ticket_with_session()
        rubric = Rubric.populate(ticket, ["AC1"])
        rubric.criteria.get(ordinal=0).record_grade(status="pass", grader_identity=_GRADER, reviewed_sha=_SHA)
        with _gate(required=True):
            check_rubric_satisfied(ticket, _SHA, transition="merge")
        assert HonestyEscalation.objects.filter(session_id=_AGENT).count() == 0

    def test_gate_off_writes_nothing(self) -> None:
        # With the gate off the check is a NO-OP — no refusal, no escalation.
        ticket = self._ticket_with_session()
        with _gate(required=False):
            check_rubric_satisfied(ticket, _SHA, transition="merge")
        assert HonestyEscalation.objects.filter(session_id=_AGENT).count() == 0


class TestRubricGradeClearsEscalation(TestCase):
    """The CLEAR landing: a fully-passed ``rubric-grade`` clears the escalation."""

    def test_fully_passed_grade_clears_active_escalation(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        Session.objects.create(overlay="t3-teatree", ticket=ticket, agent_id=_AGENT)
        HonestyEscalation.record(HonestyEscalation.Reason.SHIPPED_INCOMPLETE, session_id=_AGENT)
        Rubric.populate(ticket, ["AC1"])
        assert HonestyEscalation.is_active(_AGENT) is True

        call_command(
            "ticket",
            "rubric-grade",
            str(ticket.pk),
            "--grades-json",
            '[{"ordinal": 0, "status": "pass"}]',
            "--grader-identity",
            _GRADER,
            "--reviewed-sha",
            _SHA,
        )
        assert HonestyEscalation.is_active(_AGENT) is False

    def test_failed_grade_leaves_escalation_active(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        Session.objects.create(overlay="t3-teatree", ticket=ticket, agent_id=_AGENT)
        HonestyEscalation.record(HonestyEscalation.Reason.SHIPPED_INCOMPLETE, session_id=_AGENT)
        Rubric.populate(ticket, ["AC1", "AC2"])

        call_command(
            "ticket",
            "rubric-grade",
            str(ticket.pk),
            "--grades-json",
            '[{"ordinal": 0, "status": "fail"}]',
            "--grader-identity",
            _GRADER,
            "--reviewed-sha",
            _SHA,
        )
        # Not fully passed → the escalation is NOT cleared.
        assert HonestyEscalation.is_active(_AGENT) is True
