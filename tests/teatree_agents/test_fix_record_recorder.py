"""The fix-ticket FixRecord positive path: agent envelope -> recorder -> ticket.extra -> DELIVERED.

``fix_dod_gate`` blocks every ``kind=fix`` ticket at DELIVERED unless
``ticket.extra['fix_record']`` is complete, and nothing wrote that key — the gate's
only route through was its own override. These tests drive the whole path the
recorder now supplies, and pin the two decisions that make it safe to land on a
cross-overlay envelope contract: an ABSENT record is a no-op, a MALFORMED one is
refused by name.
"""

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.envelope_refusal import MALFORMED_FIX_RECORD_PREFIX, is_no_envelope_refusal, is_recorder_refusal
from teatree.agents.fix_record_recorder import record_returned_fix_record
from teatree.core.gates.fix_dod_gate import FixRecordDodError
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.types import FIX_RECORD_FIELDS, fix_record_missing_fields

_COMPLETE_RECORD = {
    "root_cause": "the recorder never wrote extra['fix_record']; only test factories did",
    "evidence": "grep of src/ found zero production writers of the key the gate reads",
    "regression_test": "tests/teatree_agents/test_fix_record_recorder.py::TestPositivePath",
    "observed_red": "ran against the pre-fix tree — mark_delivered raised FixRecordDodError",
    "recurrence_fingerprint": "fix_dod_gate:no_producer_for_extra_fix_record",
}


def _coding_task(*, kind: Ticket.Kind = Ticket.Kind.FIX) -> Task:
    ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, kind=kind)
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    task = Task.objects.create(ticket=ticket, session=session, phase="coding")
    task.claim(claimed_by="loop-slot")
    return task


def _coding_envelope(**extra: object) -> dict[str, object]:
    return {"summary": "fixed it", "files_modified": [{"path": "a.py", "action": "modified"}], **extra}


def _at_retrospected(ticket: Ticket) -> Ticket:
    Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.RETROSPECTED)
    ticket.refresh_from_db()
    return ticket


class TestPositivePath(TestCase):
    """A fix-ticket whose agent emitted a valid record reaches DELIVERED with NO override."""

    def test_valid_record_travels_from_envelope_to_delivered(self) -> None:
        task = _coding_task()
        record_result_envelope(task, _coding_envelope(fix_record=dict(_COMPLETE_RECORD)))
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        ticket = _at_retrospected(task.ticket)
        assert ticket.extra["fix_record"] == _COMPLETE_RECORD
        assert "fix_record_override" not in ticket.extra
        ticket.mark_delivered()
        assert ticket.state == Ticket.State.DELIVERED

    def test_without_the_recorded_record_the_same_ticket_cannot_deliver(self) -> None:
        """The mutation control for the test above — remove the write and delivery fails."""
        task = _coding_task()
        record_result_envelope(task, _coding_envelope())
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        ticket = _at_retrospected(task.ticket)
        assert "fix_record" not in (ticket.extra or {})
        with pytest.raises(FixRecordDodError):
            ticket.mark_delivered()

    def test_the_record_survives_the_intervening_fsm_transitions(self) -> None:
        """``Ticket.test()`` writes back ``_extra()``, which DROPS undeclared keys.

        Undeclared, a record written at coding would be destroyed before
        ``mark_delivered`` ever reads it — so the ``TicketExtra`` declaration is what
        makes the positive path reach the gate. Mirrors the production shape at
        ``core/models/task.py``: the transition mutates, the ``save()`` persists.
        The control key proves the strip is real rather than assumed.
        """
        task = _coding_task()
        ticket = task.ticket
        record_result_envelope(task, _coding_envelope(fix_record=dict(_COMPLETE_RECORD)))
        ticket.refresh_from_db()
        planted = {**(ticket.extra or {}), "an_undeclared_control_key": "x"}
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.CODED, extra=planted)
        ticket.refresh_from_db()
        ticket.test(passed=True)
        ticket.save()
        ticket.refresh_from_db()
        assert ticket.extra["fix_record"] == _COMPLETE_RECORD
        assert "an_undeclared_control_key" not in ticket.extra


class TestAbsentRecordIsANoOp(TestCase):
    """The compatibility decision: an overlay that never emits one must not start failing."""

    def test_a_fix_ticket_run_without_the_key_completes(self) -> None:
        task = _coding_task()
        attempt = record_result_envelope(task, _coding_envelope())
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert attempt.error == ""
        assert "fix_record" not in (task.ticket.extra or {})

    def test_the_recorder_reports_no_error_for_an_absent_key(self) -> None:
        task = _coding_task()
        assert record_returned_fix_record(task, {"summary": "x"}) == ""


class TestMalformedRecordIsRefused(TestCase):
    """A dropped record must not read as an absent one."""

    def test_a_partial_record_fails_the_task_naming_every_missing_field(self) -> None:
        task = _coding_task()
        attempt = record_result_envelope(task, _coding_envelope(fix_record={"root_cause": "x"}))
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert attempt.error.startswith(MALFORMED_FIX_RECORD_PREFIX)
        for field in ("evidence", "regression_test", "observed_red", "recurrence_fingerprint"):
            assert field in attempt.error
        assert "fix_record" not in (task.ticket.extra or {})

    def test_a_non_mapping_record_is_refused_with_every_field(self) -> None:
        for raw in ("not-a-mapping", [], 7):
            error = record_returned_fix_record(_coding_task(), {"fix_record": raw})
            assert error.startswith(MALFORMED_FIX_RECORD_PREFIX)
            assert all(field in error for field in FIX_RECORD_FIELDS)

    def test_a_blank_field_is_missing_not_present(self) -> None:
        task = _coding_task()
        error = record_returned_fix_record(task, {"fix_record": {**_COMPLETE_RECORD, "observed_red": "   "}})
        assert "observed_red" in error

    def test_the_refusal_classifies_as_a_recorder_envelope_refusal(self) -> None:
        task = _coding_task()
        error = record_returned_fix_record(task, {"fix_record": {"root_cause": "x"}})
        assert is_recorder_refusal(error) is True
        assert is_no_envelope_refusal(error) is False


class TestRecordedShape(TestCase):
    def test_only_the_declared_fields_are_stored(self) -> None:
        task = _coding_task()
        record_returned_fix_record(task, {"fix_record": {**_COMPLETE_RECORD, "stray": "should not land"}})
        task.ticket.refresh_from_db()
        assert set(task.ticket.extra["fix_record"]) == set(FIX_RECORD_FIELDS)

    def test_a_feature_ticket_record_is_still_recorded(self) -> None:
        """Kind-agnostic: the gate decides what to READ, the recorder records what it is given."""
        task = _coding_task(kind=Ticket.Kind.FEATURE)
        assert record_returned_fix_record(task, {"fix_record": dict(_COMPLETE_RECORD)}) == ""
        task.ticket.refresh_from_db()
        assert task.ticket.extra["fix_record"] == _COMPLETE_RECORD


class TestFixRecordMissingFields(TestCase):
    """The one parse the gate, the recorder and the schema all derive from."""

    def test_absent_yields_every_field(self) -> None:
        assert fix_record_missing_fields(None) == list(FIX_RECORD_FIELDS)

    def test_complete_yields_none(self) -> None:
        assert fix_record_missing_fields(_COMPLETE_RECORD) == []

    def test_order_follows_the_declaration(self) -> None:
        assert fix_record_missing_fields({}) == list(FIX_RECORD_FIELDS)
