from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.execution import execute_bound_merge
from teatree.core.merge.rules_owner_review import assert_rules_owner_review
from teatree.utils.pr_ref import PrRef

_SHA = "a" * 40
_NEXT_SHA = "b" * 40
_REF = PrRef(slug="souliane/teatree", pr_id=5473)


def _query(*, body: str = "", author: str = "souliane", head: str = _SHA) -> Mock:
    query = Mock()
    query.ref = _REF
    query.pr_changed_paths.return_value = ["skills/rules/RULES.md"]
    query.live_head_sha.return_value = head
    query.backend.list_pr_comments.return_value = [{"body": body, "user": {"login": author, "type": "User"}}]
    return query


def _owner_aliases() -> SimpleNamespace:
    return SimpleNamespace(user_identity_aliases=("souliane",))


def test_rules_change_needs_an_owner_comment_at_the_current_head() -> None:
    query = _query(body=f"Approve Rule -1 at {_NEXT_SHA}")
    with (
        patch("teatree.core.merge.rules_owner_review.get_effective_settings", return_value=_owner_aliases()),
        pytest.raises(MergePreconditionError, match="owner review"),
    ):
        assert_rules_owner_review(query, expected_head=_SHA)


def test_the_refusal_names_the_comment_that_approves_the_current_head() -> None:
    query = _query()
    with (
        patch("teatree.core.merge.rules_owner_review.get_effective_settings", return_value=_owner_aliases()),
        pytest.raises(MergePreconditionError) as refused,
    ):
        assert_rules_owner_review(query, expected_head=_SHA)
    assert f"Approve Rule -1 at {_SHA}" in str(refused.value)


def test_rules_change_rejects_a_non_owner_comment() -> None:
    query = _query(body=f"Approve Rule -1 at {_SHA}", author="other-user")
    with (
        patch("teatree.core.merge.rules_owner_review.get_effective_settings", return_value=_owner_aliases()),
        pytest.raises(MergePreconditionError, match="owner review"),
    ):
        assert_rules_owner_review(query, expected_head=_SHA)


def test_rules_change_rejects_an_unreadable_comment_list() -> None:
    query = _query()
    query.backend.list_pr_comments.side_effect = RuntimeError("forge unavailable")
    with (
        patch("teatree.core.merge.rules_owner_review.get_effective_settings", return_value=_owner_aliases()),
        pytest.raises(MergePreconditionError, match="owner review"),
    ):
        assert_rules_owner_review(query, expected_head=_SHA)


def test_rules_change_accepts_authenticated_current_head_comment() -> None:
    query = _query(body=f"Approve Rule -1 at {_SHA}")
    with patch("teatree.core.merge.rules_owner_review.get_effective_settings", return_value=_owner_aliases()):
        assert_rules_owner_review(query, expected_head=_SHA)


def test_bound_merge_holds_unreviewed_rules_even_when_not_draft() -> None:
    query = _query()
    with (
        patch("teatree.core.merge.execution.CodeHostQuery.for_ref", return_value=query),
        patch("teatree.core.merge.rules_owner_review.get_effective_settings", return_value=_owner_aliases()),
        patch("teatree.core.merge.execution.assert_merge_provenance_trusted"),
        patch("teatree.core.merge.execution.assert_review_verdict_gate"),
        patch("teatree.core.merge.execution.assert_no_active_review_lock"),
        patch("teatree.core.gates.merge_quality_gate.assert_merge_quality_verdict"),
        patch("teatree.core.merge.execution.assert_ticket_scoped_gates"),
        patch("teatree.core.merge.execution.assert_not_draft"),
        patch("teatree.core.merge.execution.assert_ci_not_failed"),
        pytest.raises(MergePreconditionError, match="owner review"),
    ):
        execute_bound_merge(ref=_REF, expected_head_oid=_SHA)
