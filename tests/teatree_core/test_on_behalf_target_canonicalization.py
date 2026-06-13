"""On-behalf approval target canonicalization (#126 round-2, gap 1).

The on-behalf gate is the single chokepoint for every post made under the
user's identity. ``OnBehalfApproval.record``/``consume``/``matches`` used a
STRICT exact-string match on ``target``, but the consume-side token is built
differently per call site:

* ``review_on_behalf.gate_target`` → ``"{repo}!{mr}"``
* ``pr.py`` post-test-plan → ``"{repo_path}!{mr_iid}"``
* ``review_request_post`` → the canonical MR URL (a full ``https://`` string)
* the signals approval-reaction → ``pull_request.url`` (a full PR URL)

A user who pre-records ``org/repo!42`` (the documented PRE-RECORD workflow)
silently fails to match a consume token built as the full MR URL — a legit
recorded approval over-denies. The fix routes every record/consume/matches
through one canonicalization that rewrites any MR/PR URL form to the stable
``<repo>!<iid>`` token while passing non-MR targets (issue URLs, Slack
threads, ``ticket:<pk>`` compounds) through unchanged.

The matrix each gap is required to assert:

* accepts a legitimately-authorized action recorded in ANY surface form;
* STILL blocks a genuine violation (no recorded approval, or wrong scope);
* the escape actually works (canonical equivalence: record matches consume);
* single-use is still enforced (no replay).
"""

import pytest

from teatree.core.models.on_behalf_approval import OnBehalfApproval, OnBehalfAudit

pytestmark = pytest.mark.django_db

# The same MR expressed in the three surface forms the consume call sites
# actually build, plus the form a user types into approve-on-behalf.
_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/42"
_MR_URL_TRAILING = "https://gitlab.com/org/repo/-/merge_requests/42/"
_MR_REF = "org/repo!42"
_PR_URL = "https://github.com/owner/repo/pull/17"
_PR_REF = "owner/repo!17"


class TestRecordCanonicalizesTarget:
    """A recorded approval stores the canonical ``<repo>!<iid>`` token."""

    def test_url_form_is_stored_canonical(self) -> None:
        approval = OnBehalfApproval.record(target=_MR_URL, action="post_comment", approver_id="souliane")
        assert approval.target == _MR_REF

    def test_pr_url_form_is_stored_canonical(self) -> None:
        approval = OnBehalfApproval.record(target=_PR_URL, action="approval_reaction", approver_id="souliane")
        assert approval.target == _PR_REF

    def test_non_mr_target_passes_through_unchanged(self) -> None:
        """Issue URLs / ticket compounds / Slack threads are not MR refs — passthrough."""
        for raw in ("ticket:123", "https://github.com/org/repo/issues/9", "C0123/16998.55"):
            approval = OnBehalfApproval.record(target=raw, action="post_in_thread", approver_id="souliane")
            assert approval.target == raw


class TestCrossFormMatchAtConsume:
    """A pre-recorded approval in ANY surface form matches the consume token."""

    def test_recorded_ref_matches_url_consume(self) -> None:
        """The documented PRE-RECORD workflow: record ``org/repo!42``, consume the URL form."""
        OnBehalfApproval.record(target=_MR_REF, action="review_request_post", approver_id="souliane")
        consumed = OnBehalfApproval.consume(_MR_URL, "review_request_post")
        assert consumed is not None
        assert consumed.target == _MR_REF

    def test_recorded_url_matches_ref_consume(self) -> None:
        """Record the full URL, consume the ``repo!iid`` form (review_on_behalf / pr.py)."""
        OnBehalfApproval.record(target=_MR_URL, action="approve", approver_id="souliane")
        consumed = OnBehalfApproval.consume(_MR_REF, "approve")
        assert consumed is not None

    def test_recorded_url_with_trailing_slash_matches(self) -> None:
        OnBehalfApproval.record(target=_MR_URL_TRAILING, action="approve", approver_id="souliane")
        consumed = OnBehalfApproval.consume(_MR_REF, "approve")
        assert consumed is not None

    def test_pr_url_recorded_matches_pr_ref_consume(self) -> None:
        """The signals approval-reaction records a PR URL; pr.py consumes ``repo!iid``."""
        OnBehalfApproval.record(target=_PR_URL, action="approval_reaction", approver_id="souliane")
        consumed = OnBehalfApproval.consume(_PR_REF, "approval_reaction")
        assert consumed is not None

    def test_matches_helper_is_cross_form(self) -> None:
        approval = OnBehalfApproval.record(target=_MR_REF, action="post_comment", approver_id="souliane")
        assert approval.matches(_MR_URL, "post_comment") is True
        assert approval.matches(_MR_URL_TRAILING, "post_comment") is True


class TestStillBlocksGenuineViolation:
    """Canonicalization must not collapse distinct scopes or grant unrecorded actions."""

    def test_no_recorded_approval_still_blocks(self) -> None:
        assert OnBehalfApproval.consume(_MR_URL, "post_comment") is None

    def test_wrong_mr_does_not_match(self) -> None:
        OnBehalfApproval.record(target=_MR_REF, action="post_comment", approver_id="souliane")
        assert OnBehalfApproval.consume("https://gitlab.com/org/repo/-/merge_requests/43", "post_comment") is None

    def test_wrong_action_does_not_match(self) -> None:
        OnBehalfApproval.record(target=_MR_URL, action="post_comment", approver_id="souliane")
        assert OnBehalfApproval.consume(_MR_REF, "approve") is None

    def test_distinct_projects_do_not_collide(self) -> None:
        """A nested-group project recorded must not satisfy the shallow form."""
        OnBehalfApproval.record(
            target="https://gitlab.com/org/sub/repo/-/merge_requests/1",
            action="approve",
            approver_id="souliane",
        )
        assert OnBehalfApproval.consume("org/repo!1", "approve") is None


class TestSingleUseStillEnforced:
    """Cross-form matching must not weaken the single-use guarantee."""

    def test_url_recorded_consumed_once_then_blocks(self) -> None:
        OnBehalfApproval.record(target=_MR_URL, action="approve", approver_id="souliane")
        first = OnBehalfApproval.consume(_MR_REF, "approve")
        assert first is not None
        # A second consume in EITHER surface form must find nothing.
        assert OnBehalfApproval.consume(_MR_REF, "approve") is None
        assert OnBehalfApproval.consume(_MR_URL, "approve") is None

    def test_audit_row_carries_canonical_target(self) -> None:
        OnBehalfApproval.record(target=_MR_URL, action="approve", approver_id="souliane")
        consumed = OnBehalfApproval.consume(_MR_REF, "approve")
        assert consumed is not None
        audit = OnBehalfAudit.objects.create(
            approval=consumed,
            target=consumed.target,
            action=consumed.action,
            approver_id=consumed.approver_id,
        )
        assert audit.target == _MR_REF
