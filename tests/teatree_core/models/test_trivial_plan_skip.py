"""Trivial-work plan-skip marker — the lightweight, audited plan-gate carve-out.

A per-ticket "this AUTHOR ticket is trivial — skip the planner" marker, modelled
exactly like the #2104 external-delivery lease (a key on ``Ticket.extra`` written
through the canonical locked ``merge_extra``), but durable (no TTL) and carrying a
MANDATORY recorded reason. ``trivial_plan_skip_reason`` returns the reason while
a well-formed marker is present, ``None`` otherwise (a malformed or empty marker
is treated as absent so a garbled row never silently skips planning).
"""

import pytest
from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.trivial_plan_skip import is_trivial_plan_skip, mark_trivial_plan_skip, trivial_plan_skip_reason


class TestTrivialPlanSkipMarker(TestCase):
    def test_unmarked_ticket_is_not_trivial_plan_skip(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        assert is_trivial_plan_skip(ticket) is False
        assert trivial_plan_skip_reason(ticket) is None

    def test_marked_ticket_is_trivial_plan_skip_and_records_reason(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        mark_trivial_plan_skip(ticket, reason="one-line typo fix in a comment", by="operator")
        ticket.refresh_from_db()
        assert is_trivial_plan_skip(ticket) is True
        assert trivial_plan_skip_reason(ticket) == "one-line typo fix in a comment"

    def test_empty_reason_is_rejected(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        with pytest.raises(ValueError, match="reason"):
            mark_trivial_plan_skip(ticket, reason="", by="operator")

    def test_whitespace_only_reason_is_rejected(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        with pytest.raises(ValueError, match="reason"):
            mark_trivial_plan_skip(ticket, reason="   ", by="operator")

    def test_empty_by_is_rejected(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        with pytest.raises(ValueError, match="by"):
            mark_trivial_plan_skip(ticket, reason="trivial", by="")

    def test_marker_persists_through_locked_rmw_without_clobbering(self) -> None:
        ticket = Ticket.objects.create(overlay="test", extra={"keep": 1})
        mark_trivial_plan_skip(ticket, reason="trivial", by="operator")
        ticket.refresh_from_db()
        assert ticket.extra["keep"] == 1
        assert "trivial_plan_skip" in ticket.extra

    def test_marker_records_audit_fields(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        mark_trivial_plan_skip(ticket, reason="trivial", by="alice")
        ticket.refresh_from_db()
        marker = ticket.extra["trivial_plan_skip"]
        assert marker["reason"] == "trivial"
        assert marker["by"] == "alice"
        assert marker["at"]  # ISO timestamp recorded

    def test_marker_with_empty_reason_value_is_treated_as_absent(self) -> None:
        ticket = Ticket.objects.create(overlay="test", extra={"trivial_plan_skip": {"reason": "", "by": "x"}})
        assert is_trivial_plan_skip(ticket) is False
        assert trivial_plan_skip_reason(ticket) is None

    def test_marker_missing_reason_is_treated_as_absent(self) -> None:
        ticket = Ticket.objects.create(overlay="test", extra={"trivial_plan_skip": {"by": "x"}})
        assert is_trivial_plan_skip(ticket) is False

    def test_non_dict_marker_value_is_treated_as_absent(self) -> None:
        ticket = Ticket.objects.create(overlay="test", extra={"trivial_plan_skip": "garbage"})
        assert is_trivial_plan_skip(ticket) is False
