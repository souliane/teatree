"""Tests for teatree.core.gates.fix_dod_gate — the fix-ticket FixRecord DoD merge gate.

The gate's pure helpers (``is_fix``, ``override_reason``,
``missing_fix_record_fields``, ``has_valid_fix_record``, ``check_fix_record_dod``)
are exercised directly; the FSM wiring is exercised through
``Ticket.mark_delivered`` so a fix without a validated FixRecord cannot reach
DELIVERED.
"""

import pytest
from django.test import TestCase

from teatree.core.gates.fix_dod_gate import (
    FixRecordDodError,
    check_fix_record_dod,
    has_valid_fix_record,
    is_fix,
    missing_fix_record_fields,
    override_reason,
)
from teatree.core.models import Ticket

_COMPLETE_RECORD = {
    "root_cause": "carve-out resolved repo from ambient cwd, ignoring git -C target",
    "evidence": "sub-agent commit to a verified-private repo was over-blocked; cwd reset between shells",
    "regression_test": "tests/test_publish_surface.py::TestEffectiveRepoDir::test_dash_c_separate_value",
    "observed_red": "ran against pre-fix SHA d4bd513 — FAILED with over-block",
    "recurrence_fingerprint": "publish_surface:commit_repo_cwd_vs_dash_c",
}


def _fix_ticket(**extra: object) -> Ticket:
    return Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FIX, extra=dict(extra))


class TestIsFix(TestCase):
    def test_fix_kind_is_governed(self) -> None:
        assert is_fix(Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FIX)) is True

    def test_feature_kind_is_not_governed(self) -> None:
        assert is_fix(Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FEATURE)) is False

    def test_default_kind_is_feature(self) -> None:
        assert Ticket.objects.create(overlay="acme").kind == Ticket.Kind.FEATURE


class TestMissingFixRecordFields(TestCase):
    def test_no_record_means_all_fields_missing(self) -> None:
        ticket = _fix_ticket()
        assert set(missing_fix_record_fields(ticket)) == {
            "root_cause",
            "evidence",
            "regression_test",
            "observed_red",
            "recurrence_fingerprint",
        }

    def test_non_mapping_record_means_all_fields_missing(self) -> None:
        ticket = _fix_ticket(fix_record="not-a-dict")
        assert len(missing_fix_record_fields(ticket)) == 5

    def test_partial_record_reports_only_the_gaps(self) -> None:
        ticket = _fix_ticket(fix_record={"root_cause": "x", "evidence": "y"})
        assert set(missing_fix_record_fields(ticket)) == {
            "regression_test",
            "observed_red",
            "recurrence_fingerprint",
        }

    def test_blank_field_counts_as_missing(self) -> None:
        record = {**_COMPLETE_RECORD, "observed_red": "   "}
        ticket = _fix_ticket(fix_record=record)
        assert missing_fix_record_fields(ticket) == ["observed_red"]

    def test_complete_record_has_no_gaps(self) -> None:
        ticket = _fix_ticket(fix_record=_COMPLETE_RECORD)
        assert missing_fix_record_fields(ticket) == []
        assert has_valid_fix_record(ticket) is True


class TestOverrideReason(TestCase):
    def test_absent_override_is_empty(self) -> None:
        assert override_reason(_fix_ticket()) == ""

    def test_recorded_reason_is_returned(self) -> None:
        ticket = _fix_ticket(fix_record_override={"reason": "trivial one-char typo, no root cause to state"})
        assert override_reason(ticket) == "trivial one-char typo, no root cause to state"


class TestCheckFixRecordDod(TestCase):
    def test_feature_ticket_passes_without_a_record(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FEATURE)
        check_fix_record_dod(ticket)  # does not raise

    def test_fix_with_complete_record_passes(self) -> None:
        check_fix_record_dod(_fix_ticket(fix_record=_COMPLETE_RECORD))

    def test_fix_with_override_passes(self) -> None:
        check_fix_record_dod(_fix_ticket(fix_record_override={"reason": "exempt"}))

    def test_fix_without_record_is_refused(self) -> None:
        with pytest.raises(FixRecordDodError):
            check_fix_record_dod(_fix_ticket())

    def test_fix_with_partial_record_is_refused(self) -> None:
        with pytest.raises(FixRecordDodError):
            check_fix_record_dod(_fix_ticket(fix_record={"root_cause": "x"}))

    def test_refusal_names_the_missing_fields(self) -> None:
        with pytest.raises(FixRecordDodError) as exc:
            check_fix_record_dod(_fix_ticket(fix_record={"root_cause": "x"}))
        assert "recurrence_fingerprint" in str(exc.value)


class TestMarkDeliveredFsmGate(TestCase):
    def _retrospected(self, **kwargs: object) -> Ticket:
        return Ticket.objects.create(overlay="acme", state=Ticket.State.RETROSPECTED, **kwargs)

    def test_feature_ticket_delivers(self) -> None:
        ticket = self._retrospected(kind=Ticket.Kind.FEATURE)
        ticket.mark_delivered()
        assert ticket.state == Ticket.State.DELIVERED

    def test_fix_with_record_delivers(self) -> None:
        ticket = self._retrospected(kind=Ticket.Kind.FIX, extra={"fix_record": _COMPLETE_RECORD})
        ticket.mark_delivered()
        assert ticket.state == Ticket.State.DELIVERED

    def test_fix_without_record_cannot_deliver(self) -> None:
        ticket = self._retrospected(kind=Ticket.Kind.FIX)
        with pytest.raises(FixRecordDodError):
            ticket.mark_delivered()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.RETROSPECTED

    def test_fix_with_override_delivers(self) -> None:
        ticket = self._retrospected(kind=Ticket.Kind.FIX, extra={"fix_record_override": {"reason": "exempt"}})
        ticket.mark_delivered()
        assert ticket.state == Ticket.State.DELIVERED
