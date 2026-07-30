"""The §17.4.3 authorization guard facets + the merge-chokepoint floor gates.

``_assert_clear_authorized`` was one deeply-branching block; it is now the ordered
composition of four named facets (actionable → verdict-not-smuggled →
reviewer-independent → substrate-authorized). These tests pin each facet in
isolation AND the refusal ORDER of the composition, so a future edit that reorders
or drops a guard fails here. ``assert_not_draft`` / ``assert_ci_not_failed`` are
the last-line floor gates the chokepoint registry references (§3b #1).

Rows are written straight via ``.objects.create()`` — the exact fixture/raw-ORM
path the merge-time re-checks defend against, so the guards are the thing under
test, not the factory ``issue()`` guard.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge.authorization import (
    _assert_clear_actionable,
    _assert_clear_authorized,
    _assert_reviewer_independent,
    _assert_substrate_authorized,
    _assert_verdict_not_smuggled,
)
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.execution import assert_ci_not_failed, assert_not_draft
from teatree.core.models import MergeClear
from teatree.utils.pr_ref import PrRef

_SHA = "a" * 40
_SLUG = "souliane/teatree"


def _clear(**overrides: object) -> MergeClear:
    defaults: dict[str, object] = {
        "pr_id": 42,
        "slug": _SLUG,
        "reviewed_sha": _SHA,
        "reviewer_identity": "cold-reviewer",
        "gh_verify_result": MergeClear.VerifyResult.GREEN,
        "blast_class": MergeClear.BlastClass.LOGIC,
    }
    defaults.update(overrides)
    return MergeClear.objects.create(**defaults)


# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestActionableFacet(TestCase):
    def test_non_mergeclear_is_refused(self) -> None:
        with pytest.raises(MergePreconditionError, match="no MergeClear row"):
            _assert_clear_actionable(object(), slug=_SLUG, pr_id=42)

    def test_actionable_clear_is_returned_narrowed(self) -> None:
        clear = _clear()
        assert _assert_clear_actionable(clear, slug=_SLUG, pr_id=42) is clear


class TestVerdictSmuggleFacet(TestCase):
    def test_failed_verdict_is_refused_unconditionally(self) -> None:
        clear = _clear(gh_verify_result=MergeClear.VerifyResult.FAILED)
        with pytest.raises(MergePreconditionError, match="gh_verify_result=failed"):
            _assert_verdict_not_smuggled(clear, slug=_SLUG, pr_id=42, expedite="")

    def test_failed_verdict_refused_even_with_expedite_waiver(self) -> None:
        # A FAILED verdict is a real red — expedite can NEVER waive it.
        clear = _clear(gh_verify_result=MergeClear.VerifyResult.FAILED)
        with pytest.raises(MergePreconditionError, match="gh_verify_result=failed"):
            _assert_verdict_not_smuggled(clear, slug=_SLUG, pr_id=42, expedite="anyone")

    def test_pending_without_waiver_is_refused(self) -> None:
        clear = _clear(gh_verify_result=MergeClear.VerifyResult.PENDING)
        with pytest.raises(MergePreconditionError, match="not green"):
            _assert_verdict_not_smuggled(clear, slug=_SLUG, pr_id=42, expedite="")

    def test_green_verdict_passes(self) -> None:
        _assert_verdict_not_smuggled(_clear(), slug=_SLUG, pr_id=42, expedite="")


class TestReviewerIndependentFacet(TestCase):
    def test_reviewer_equal_to_loop_is_refused(self) -> None:
        clear = _clear(reviewer_identity="loop-session")
        with pytest.raises(MergePreconditionError, match="equals the executing loop"):
            _assert_reviewer_independent(clear, executing_loop_identity="loop-session")

    def test_non_reviewer_role_is_refused(self) -> None:
        clear = _clear(reviewer_identity="maker-bot")
        with pytest.raises(MergePreconditionError, match="non-reviewer role"):
            _assert_reviewer_independent(clear, executing_loop_identity="merge-loop")

    def test_independent_reviewer_passes(self) -> None:
        _assert_reviewer_independent(_clear(), executing_loop_identity="merge-loop")


class TestSubstrateFacet(TestCase):
    def test_substrate_without_human_authoriser_is_held(self) -> None:
        clear = _clear(blast_class=MergeClear.BlastClass.SUBSTRATE)
        with pytest.raises(MergePreconditionError, match="blast_class=substrate"):
            _assert_substrate_authorized(clear, slug=_SLUG, pr_id=42, human="")

    def test_human_authorized_presented_for_non_substrate_is_refused(self) -> None:
        with pytest.raises(MergePreconditionError, match="non-substrate"):
            _assert_substrate_authorized(_clear(), slug=_SLUG, pr_id=42, human="the-user")

    def test_non_substrate_no_human_passes(self) -> None:
        _assert_substrate_authorized(_clear(), slug=_SLUG, pr_id=42, human="")


class TestRefusalOrderOfTheComposition(TestCase):
    def test_verdict_smuggle_refused_before_reviewer_check(self) -> None:
        # A clear that BOTH records a FAILED verdict AND is self-attested (reviewer ==
        # loop) must refuse on the verdict facet — it runs before the reviewer facet.
        clear = _clear(gh_verify_result=MergeClear.VerifyResult.FAILED, reviewer_identity="loop-session")
        with pytest.raises(MergePreconditionError, match="gh_verify_result=failed"):
            _assert_clear_authorized(
                clear=clear,
                executing_loop_identity="loop-session",
                slug=_SLUG,
                pr_id=42,
            )

    def test_actionable_refused_before_verdict_check(self) -> None:
        # A non-MergeClear cannot even be inspected for a verdict — the actionable
        # facet fails first with the step-1 "no MergeClear row" message.
        with pytest.raises(MergePreconditionError, match="no MergeClear row"):
            _assert_clear_authorized(
                clear=object(),
                executing_loop_identity="merge-loop",
                slug=_SLUG,
                pr_id=42,
            )


class TestFloorGates(TestCase):
    def _query(self) -> CodeHostQuery:
        return CodeHostQuery.for_ref(PrRef(slug=_SLUG, pr_id=42))

    def test_assert_not_draft_refuses_a_draft_head(self) -> None:
        with (
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.pr_is_draft", return_value=True),
            pytest.raises(MergePreconditionError, match="draft state"),
        ):
            assert_not_draft(self._query())

    def test_assert_not_draft_passes_a_non_draft_head(self) -> None:
        with patch("teatree.core.merge.ci_rollup.CodeHostQuery.pr_is_draft", return_value=False):
            assert_not_draft(self._query())

    def test_assert_ci_not_failed_refuses_a_failed_verdict(self) -> None:
        with (
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.required_checks_status", return_value="failed"),
            pytest.raises(MergePreconditionError, match="are failed"),
        ):
            assert_ci_not_failed(self._query())

    def test_assert_ci_not_failed_allows_pending_at_the_floor(self) -> None:
        # The floor refuses only a FAILED verdict; a PENDING head is allowed here
        # (the expedite waiver was already resolved in assert_merge_preconditions).
        with patch("teatree.core.merge.ci_rollup.CodeHostQuery.required_checks_status", return_value="pending"):
            assert_ci_not_failed(self._query())

    def test_assert_ci_not_failed_allows_green(self) -> None:
        with patch("teatree.core.merge.ci_rollup.CodeHostQuery.required_checks_status", return_value="green"):
            assert_ci_not_failed(self._query())
