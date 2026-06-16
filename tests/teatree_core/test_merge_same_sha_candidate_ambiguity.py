"""Same-SHA multi-candidate ambiguity must fail loud, never silently bind (#2338).

The #1335 cross-repo probe (``_probe_candidate_repos``) recovers the repo whose
PR #N head matches the reviewed SHA when the initially-resolved repo's PR is an
unrelated same-numbered PR. The #2327 candidate-set widening (overlay
working-repos) made it possible for TWO distinct candidate repos to expose PR #N
at the SAME reviewed SHA — a fork/mirror, or a working-repo aliasing another. The
pre-fix probe returned the FIRST match, so the merge silently bound to whichever
was probed first, merging a potentially-unverified twin.

The fix collects EVERY candidate whose PR #N head matches the reviewed SHA and
requires EXACTLY ONE: a multi-match raises a :class:`MergePreconditionError`
naming every ambiguous repo, never silently picks the first. The #1335
different-SHA guard (a same-numbered PR at the WRONG SHA still raises, naming
candidates) and the best-effort per-candidate swallow contract are preserved.

The concrete repo names are neutral placeholders — core/tests stay
overlay-agnostic (BLUEPRINT § 1). Only the forge subprocess (the network
boundary) is stubbed; the candidate enumeration and reconciliation run through
real teatree code.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge import MergePreconditionError, pr_slug_resolution

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_REVIEWED_SHA = "a" * 40  # the reviewed SHA carried by the CLEAR
_WRONG_SHA = "b" * 40  # an unrelated same-numbered PR at the wrong SHA
_PR_ID = 159

_INITIAL_SLUG = "souliane/teatree"  # the clone-origin (wrong) repo, probed first
_REPO_ONE = "downstream-org/downstream-overlay"
_REPO_TWO = "mirror-org/downstream-overlay-fork"


def _head_by_repo(matching: set[str]):
    """A ``fetch_live_head_sha`` stub returning the reviewed SHA for *matching* repos.

    Every repo in *matching* exposes PR #N at ``_REVIEWED_SHA``; every other repo
    exposes it at ``_WRONG_SHA``. Which repo "owns" the reviewed work is decided
    purely by membership in *matching*, so a same-SHA ambiguity is modelled by
    putting two repos in the set.
    """

    def _fetch(slug: str, pr_id: int, *, host_kind: str = "github") -> str:
        del pr_id, host_kind
        return _REVIEWED_SHA if slug in matching else _WRONG_SHA

    return _fetch


class TestSameShaMultiCandidateRaises(TestCase):
    """#2338: two candidates at the SAME reviewed SHA → raise naming both."""

    def test_probe_returns_every_matching_candidate(self) -> None:
        """The probe collects ALL matches, not just the first (the unit contract).

        This is the assertion that goes RED on the pre-fix code: the old probe
        returned a single ``str`` (the first match), so it could never surface
        the second matching repo. Collecting every match is what lets the caller
        detect the ambiguity.
        """
        with patch(
            "teatree.core.merge.pr_slug_resolution.fetch_live_head_sha",
            side_effect=_head_by_repo({_REPO_ONE, _REPO_TWO}),
        ):
            matches = pr_slug_resolution._probe_candidate_repos(
                pr_id=_PR_ID,
                reviewed_sha=_REVIEWED_SHA,
                candidates=[_REPO_ONE, _REPO_TWO],
                host_kind="github",
            )

        assert matches == [_REPO_ONE, _REPO_TWO], (
            f"probe must return every candidate whose head matches the reviewed SHA; got {matches!r}"
        )

    def test_reconcile_raises_naming_every_ambiguous_repo(self) -> None:
        """Two candidate repos at the reviewed SHA → reconciliation RAISES naming both.

        This is the #2338 must-raise: the initial (clone-origin) repo's PR #N is
        at the wrong SHA, and TWO distinct candidate repos expose PR #N at the
        reviewed SHA. The pre-fix code silently returned the first; the fix
        refuses, naming every ambiguous repo so the operator can re-issue an
        explicit-slug CLEAR.
        """
        with (
            patch(
                "teatree.core.merge.pr_slug_resolution.fetch_live_head_sha",
                side_effect=_head_by_repo({_REPO_ONE, _REPO_TWO}),
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs",
                return_value=[_INITIAL_SLUG, _REPO_ONE, _REPO_TWO],
            ),
            pytest.raises(MergePreconditionError) as exc,
        ):
            pr_slug_resolution._reconcile_slug_against_reviewed_sha(
                initial_slug=_INITIAL_SLUG,
                pr_id=_PR_ID,
                reviewed_sha=_REVIEWED_SHA,
                host_kind="github",
            )

        message = str(exc.value)
        assert _REPO_ONE in message, f"ambiguity error must name the first matching repo; got: {message}"
        assert _REPO_TWO in message, f"ambiguity error must name the second matching repo; got: {message}"
        assert "ambiguous" in message.lower()


class TestSingleMatchHappyPathPreserved(TestCase):
    """Regression: exactly one candidate at the reviewed SHA still resolves."""

    def test_probe_returns_the_single_match(self) -> None:
        with patch(
            "teatree.core.merge.pr_slug_resolution.fetch_live_head_sha",
            side_effect=_head_by_repo({_REPO_ONE}),
        ):
            matches = pr_slug_resolution._probe_candidate_repos(
                pr_id=_PR_ID,
                reviewed_sha=_REVIEWED_SHA,
                candidates=[_REPO_ONE, _REPO_TWO],
                host_kind="github",
            )

        assert matches == [_REPO_ONE]

    def test_reconcile_recovers_the_single_matching_repo(self) -> None:
        """One candidate matches the reviewed SHA → reconciliation returns it (no raise)."""
        with (
            patch(
                "teatree.core.merge.pr_slug_resolution.fetch_live_head_sha",
                side_effect=_head_by_repo({_REPO_ONE}),
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs",
                return_value=[_INITIAL_SLUG, _REPO_ONE, _REPO_TWO],
            ),
        ):
            resolved = pr_slug_resolution._reconcile_slug_against_reviewed_sha(
                initial_slug=_INITIAL_SLUG,
                pr_id=_PR_ID,
                reviewed_sha=_REVIEWED_SHA,
                host_kind="github",
            )

        assert resolved == _REPO_ONE


class TestDifferentShaGuardPreserved(TestCase):
    """Regression: the #1335 different-SHA guard still raises, naming candidates."""

    def test_probe_returns_empty_when_no_candidate_matches(self) -> None:
        with patch(
            "teatree.core.merge.pr_slug_resolution.fetch_live_head_sha",
            side_effect=_head_by_repo(set()),
        ):
            matches = pr_slug_resolution._probe_candidate_repos(
                pr_id=_PR_ID,
                reviewed_sha=_REVIEWED_SHA,
                candidates=[_REPO_ONE, _REPO_TWO],
                host_kind="github",
            )

        assert matches == []

    def test_reconcile_raises_head_moved_naming_candidates(self) -> None:
        """No candidate carries the reviewed SHA → the #1335 'head moved' raise.

        The initial repo's PR #N and both candidates are all at the WRONG SHA;
        the gate must still fail loud (a real force-push or a stale CLEAR),
        naming every candidate considered — never silently binding a
        same-numbered PR that does not carry the reviewed work.
        """
        with (
            patch(
                "teatree.core.merge.pr_slug_resolution.fetch_live_head_sha",
                side_effect=_head_by_repo(set()),
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs",
                return_value=[_INITIAL_SLUG, _REPO_ONE, _REPO_TWO],
            ),
            pytest.raises(MergePreconditionError) as exc,
        ):
            pr_slug_resolution._reconcile_slug_against_reviewed_sha(
                initial_slug=_INITIAL_SLUG,
                pr_id=_PR_ID,
                reviewed_sha=_REVIEWED_SHA,
                host_kind="github",
            )

        message = str(exc.value)
        assert "PR head moved" in message
        assert _INITIAL_SLUG in message
        assert _REPO_ONE in message
        assert _REPO_TWO in message
