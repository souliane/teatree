"""Recorded per-post user-approval channel for the on-behalf pre-gate (#960/#961).

Mirrors the #953 ``DbApproval``/``DbAudit`` safety model 1:1 for the
on-behalf post gate: a durable, single-use, scoped, maker≠checker user
approval the gate consumes so a chat-only operator can satisfy the gate
without a TTY — never a silent drop, never an unattended post.
"""

import pytest

from teatree.core.models.on_behalf_approval import OnBehalfApproval, OnBehalfApprovalError, OnBehalfAudit

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestOnBehalfApprovalRecord:
    def test_record_creates_a_row(self) -> None:
        approval = OnBehalfApproval.record(
            target="github.com/org/repo#42",
            action="post_comment",
            approver_id="souliane",
        )
        assert approval.pk is not None
        assert approval.consumed_at is None
        assert approval.approver_id == "souliane"

    def test_record_strips_and_requires_target(self) -> None:
        with pytest.raises(OnBehalfApprovalError, match="target is required"):
            OnBehalfApproval.record(target="  ", action="post_comment", approver_id="souliane")

    def test_record_requires_action(self) -> None:
        with pytest.raises(OnBehalfApprovalError, match="action is required"):
            OnBehalfApproval.record(target="t#1", action="", approver_id="souliane")

    def test_record_requires_approver(self) -> None:
        with pytest.raises(OnBehalfApprovalError, match="approver_id is required"):
            OnBehalfApproval.record(target="t#1", action="post_comment", approver_id="")

    def test_record_refuses_a_maker_or_agent_approver(self) -> None:
        """The executing agent can never self-authorize an on-behalf post."""
        for role in ("maker:abc", "coding-agent", "loop"):
            with pytest.raises(OnBehalfApprovalError, match="maker/coding-agent/loop"):
                OnBehalfApproval.record(target="t#1", action="post_comment", approver_id=role)


class TestOnBehalfApprovalMatches:
    def test_matches_exact_scope_only(self) -> None:
        approval = OnBehalfApproval.record(target="t#1", action="post_comment", approver_id="souliane")
        assert approval.matches("t#1", "post_comment") is True
        assert approval.matches("t#2", "post_comment") is False
        assert approval.matches("t#1", "reply_to_discussion") is False

    def test_consumed_row_never_matches(self) -> None:
        approval = OnBehalfApproval.record(target="t#1", action="post_comment", approver_id="souliane")
        OnBehalfApproval.consume("t#1", "post_comment")
        approval.refresh_from_db()
        assert approval.matches("t#1", "post_comment") is False


class TestOnBehalfApprovalConsume:
    def test_consume_returns_and_marks_single_use(self) -> None:
        OnBehalfApproval.record(target="t#1", action="post_comment", approver_id="souliane")
        first = OnBehalfApproval.consume("t#1", "post_comment")
        assert first is not None
        assert first.consumed_at is not None
        second = OnBehalfApproval.consume("t#1", "post_comment")
        assert second is None

    def test_consume_returns_none_when_no_recorded_approval(self) -> None:
        assert OnBehalfApproval.consume("t#1", "post_comment") is None


class TestOnBehalfAudit:
    def test_audit_row_records_who_what_when(self) -> None:
        OnBehalfApproval.record(target="t#9", action="post_dm_noop", approver_id="souliane")
        consumed = OnBehalfApproval.consume("t#9", "post_dm_noop")
        assert consumed is not None
        audit = OnBehalfAudit.objects.create(
            approval=consumed,
            target=consumed.target,
            action=consumed.action,
            approver_id=consumed.approver_id,
        )
        assert audit.approver_id == "souliane"
        assert audit.target == "t#9"


class TestStrRepresentations:
    def test_approval_str(self) -> None:
        approval = OnBehalfApproval.record(target="org/repo#1", action="post_comment", approver_id="souliane")
        assert str(approval) == "on-behalf-approval<post_comment:org/repo#1 by souliane>"

    def test_audit_str(self) -> None:
        OnBehalfApproval.record(target="t#1", action="post_comment", approver_id="souliane")
        consumed = OnBehalfApproval.consume("t#1", "post_comment")
        assert consumed is not None
        audit = OnBehalfAudit.objects.create(
            approval=consumed,
            target=consumed.target,
            action=consumed.action,
            approver_id=consumed.approver_id,
        )
        assert str(audit) == "on-behalf-audit<post_comment:t#1 by souliane>"
