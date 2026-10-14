import pytest
from django.test import TestCase

from teatree.core.gates.exact_approval import (
    ExactApprovalMissingError,
    ExactApprovalStaleError,
    approval_hint,
    spent_approval,
    write_target,
)
from teatree.core.models import OnBehalfApproval, OnBehalfAudit


class TestExactApproval(TestCase):
    def test_digest_binds_object_and_text(self) -> None:
        target = write_target("slack", "C1/1.2", "before", "after")

        assert target.startswith("slack:C1/1.2:")
        assert target != write_target("slack", "C1/1.2", "before", "other")
        assert approval_hint(target, "slack_delete")["record_command"].startswith("t3 review approve-on-behalf")

    def test_missing_and_stale_approvals_refuse_without_a_write(self) -> None:
        target = write_target("slack", "C1/1.2", "before", "after")
        with pytest.raises(ExactApprovalMissingError), spent_approval(target, "slack_delete"):
            pytest.fail("an unapproved mutation ran")

        OnBehalfApproval.record(write_target("slack", "C1/1.2", "before", "other"), "slack_delete", "owner")
        with pytest.raises(ExactApprovalStaleError), spent_approval(target, "slack_delete"):
            pytest.fail("a stale-approved mutation ran")
        assert not OnBehalfAudit.objects.exists()

    def test_spends_one_exact_approval_and_audits(self) -> None:
        target = write_target("slack", "C1/1.2", "before", "after")
        approval = OnBehalfApproval.record(target, "slack_delete", "owner")

        with spent_approval(target, "slack_delete"):
            pass

        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.get().approval_id == approval.pk
