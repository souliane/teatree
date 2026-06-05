"""Single-use user-approval channel for the mandatory-E2E bypass (#1967).

``E2EBypassApproval`` mirrors ``OnBehalfApproval`` (#960) / ``DbApproval``
(#953) / ``MergeClear`` (§17.4): a durable, single-use, strictly scoped row,
creatable only through the guarded ``record`` factory which refuses a
maker/coding-agent/loop approver (the executing agent can never authorize its
own E2E bypass — maker≠checker). The scope is the ticket plus the reviewed head
SHA, so a bypass binds to one tree and never carries to a later commit.
"""

import pytest
from django.test import TestCase

from teatree.core.models import E2EBypassApproval, E2EBypassApprovalError, E2EBypassAudit, Ticket

_SHA = "a" * 40
_USER = "souliane"


class TestRecordGuard(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/1")

    def test_record_writes_a_row(self) -> None:
        approval = E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        assert approval.pk is not None
        assert approval.head_sha == _SHA
        assert approval.approver_id == _USER
        assert approval.consumed_at is None

    def test_record_refuses_maker_approver(self) -> None:
        with pytest.raises(E2EBypassApprovalError):
            E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id="maker")

    def test_record_refuses_loop_approver(self) -> None:
        with pytest.raises(E2EBypassApprovalError):
            E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id="merge-loop")

    def test_record_refuses_coding_agent_approver(self) -> None:
        with pytest.raises(E2EBypassApprovalError):
            E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id="coding-agent")

    def test_record_refuses_empty_approver(self) -> None:
        with pytest.raises(E2EBypassApprovalError):
            E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id="   ")

    def test_record_refuses_short_sha(self) -> None:
        with pytest.raises(E2EBypassApprovalError):
            E2EBypassApproval.record(ticket=self.ticket, head_sha="abc123", approver_id=_USER)

    def test_record_normalizes_sha_to_lowercase(self) -> None:
        approval = E2EBypassApproval.record(ticket=self.ticket, head_sha="A" * 40, approver_id=_USER)
        assert approval.head_sha == "a" * 40


class TestSingleUseScope(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/2")

    def test_has_unconsumed_true_after_record(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        assert E2EBypassApproval.has_unconsumed(self.ticket, _SHA) is True

    def test_has_unconsumed_false_for_other_sha(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        assert E2EBypassApproval.has_unconsumed(self.ticket, "b" * 40) is False

    def test_has_unconsumed_false_for_other_ticket(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        other = Ticket.objects.create(issue_url="https://example.com/i/3")
        assert E2EBypassApproval.has_unconsumed(other, _SHA) is False

    def test_consume_returns_row_then_marks_consumed(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        consumed = E2EBypassApproval.consume(self.ticket, _SHA)
        assert consumed is not None
        assert consumed.consumed_at is not None
        assert E2EBypassApproval.has_unconsumed(self.ticket, _SHA) is False

    def test_consume_twice_returns_none_second_time(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        assert E2EBypassApproval.consume(self.ticket, _SHA) is not None
        assert E2EBypassApproval.consume(self.ticket, _SHA) is None

    def test_consume_none_when_no_approval(self) -> None:
        assert E2EBypassApproval.consume(self.ticket, _SHA) is None

    def test_consume_writes_audit_via_caller(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        consumed = E2EBypassApproval.consume(self.ticket, _SHA)
        assert consumed is not None
        E2EBypassAudit.objects.create(
            approval=consumed,
            ticket=self.ticket,
            head_sha=consumed.head_sha,
            approver_id=consumed.approver_id,
        )
        assert E2EBypassAudit.objects.filter(approval=consumed).count() == 1
