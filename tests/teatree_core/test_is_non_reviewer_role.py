"""Unit and integration regression tests for ``is_non_reviewer_role`` (§17.8 clause 3).

The helper is the single source of truth behind the maker≠checker DOUBLE-guard:
``MergeClear.issue`` (issue-time) and ``merge_ticket_pr`` (merge-time) both call
it to reject a CLEAR whose ``reviewer_identity`` is a maker/coding-agent/loop
role.  The fix for issue #1600 extends the helper to catch loop-role identities
that appear as a delimited *component* (e.g. "merge-loop"), not only as a
leading prefix.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge import MergePreconditionError, merge_ticket_pr
from teatree.core.models import MergeClear, Ticket
from teatree.core.models.merge_clear import ClearIssuanceError, ClearRequest, is_non_reviewer_role

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # #1773 public-repo author gate — exercised by test_merge_execution_author_gate;
    # these pre-date it and target other concerns, so it is a no-op here.
    monkeypatch.setattr("teatree.core.merge.execution.assert_public_repo_author_trusted", lambda **_: None)


_SHA = "d" * 40
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'


def _gh_stub(argv: list[str]) -> tuple[int, str, str]:
    joined = " ".join(argv)
    if "headRefOid" in joined:
        return (0, _SHA, "")
    if "isDraft" in joined:
        return (0, "false", "")
    if "statusCheckRollup" in joined:
        return (0, _GREEN, "")
    if "baseRefName" in joined or "required_status_checks" in joined:
        # Base branch "main"; empty required-context gate → live rollup verdict stands.
        return (0, "main" if "baseRefName" in joined else '{"contexts": []}', "")
    if "pulls" in joined and "merge" in joined:
        return (0, '{"sha": "landed00deadbeef"}', "")
    return (0, "", "")


class TestIsNonReviewerRoleUnit(TestCase):
    """Exhaustive contract for the ``is_non_reviewer_role`` helper."""

    # --- must return True ---

    def test_merge_loop_is_blocked(self) -> None:
        assert is_non_reviewer_role("merge-loop") is True

    def test_other_loop_is_blocked(self) -> None:
        assert is_non_reviewer_role("other-loop") is True

    def test_bare_loop_is_blocked(self) -> None:
        assert is_non_reviewer_role("loop") is True

    def test_loop_prefix_worker_is_blocked(self) -> None:
        assert is_non_reviewer_role("loop-worker") is True

    def test_merge_loop_uppercase_is_blocked(self) -> None:
        assert is_non_reviewer_role("MERGE-LOOP") is True

    def test_maker_colon_prefix_is_blocked(self) -> None:
        assert is_non_reviewer_role("maker:x") is True

    def test_maker_dash_prefix_is_blocked(self) -> None:
        assert is_non_reviewer_role("maker-x") is True

    def test_bare_coding_is_blocked(self) -> None:
        assert is_non_reviewer_role("coding") is True

    def test_coding_agent_is_blocked(self) -> None:
        assert is_non_reviewer_role("coding-agent") is True

    # --- must return False ---

    def test_cold_review_reviewer_is_allowed(self) -> None:
        assert is_non_reviewer_role("reviewer:claude-cold-review") is False

    def test_codex_cli_reviewer_is_allowed(self) -> None:
        assert is_non_reviewer_role("reviewer:codex-cli") is False

    def test_human_reviewer_is_allowed(self) -> None:
        assert is_non_reviewer_role("reviewer:human-alice") is False

    def test_empty_string_is_allowed(self) -> None:
        assert is_non_reviewer_role("") is False

    def test_incidental_substring_not_blocked(self) -> None:
        # "decoding" contains "coding" as a substring but NOT as a delimited component.
        assert is_non_reviewer_role("decoding") is False


class TestIssueTimeMergeLoopBlockedIntegration(TestCase):
    """Regression: ``MergeClear.issue`` rejects an executor-role ``reviewer_identity``."""

    def test_issue_with_merge_loop_reviewer_raises(self) -> None:
        with pytest.raises(ClearIssuanceError, match=r"reviewer"):
            MergeClear.issue(
                ClearRequest(
                    pr_id=1600,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    reviewer_identity="merge-loop",
                    executing_loop_identity="other-loop",
                    gh_verify_result="green",
                    blast_class="logic",
                )
            )
        assert MergeClear.objects.count() == 0


class TestMergeTimeMergeLoopBlockedIntegration(TestCase):
    """Regression: merge-time guard rejects a CLEAR written directly via ORM with executor-role reviewer.

    A row inserted via ``objects.create`` bypasses ``MergeClear.issue``.
    The merge-time guard must independently refuse it so the double-guard
    cannot be bypassed by writing the row directly.
    """

    def test_merge_with_merge_loop_reviewer_raises(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=1601,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="merge-loop",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub),
            pytest.raises(MergePreconditionError),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="other-loop")
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None


class TestLegitimateReviewerIdentityPositiveControl(TestCase):
    """Positive control: a legitimate ``reviewer:claude-cold-review`` CLEAR issues and merges."""

    def test_cold_review_identity_issues_and_merges(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=1602,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="reviewer:claude-cold-review",
                executing_loop_identity="merge-loop",
                gh_verify_result="green",
                blast_class="logic",
                ticket=ticket,
            )
        )
        assert clear.pk is not None
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert outcome.merged_sha
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
