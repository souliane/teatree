"""The lease covers branch/PR work, not just the ticket claim (#3561).

Raw git/PR work — a hand-opened PR, an ad-hoc worktree, a direct branch push —
never entered the lifecycle, so the loop and an interactive session could claim
the same ticket and push divergent commits to one branch invisibly. A claim
registered at the PR-create / worktree-adopt seams now makes both actors
mutually visible, and the lifecycle claim DEFERS to a live foreign holder.
"""

import datetime as dt
from typing import TYPE_CHECKING, cast
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ImplementedIssueMarker, LoopLease
from teatree.core.work_lease import (
    WorkIdentity,
    branch_slot,
    foreign_work_holder,
    issue_slot,
    pr_slot,
    register_work_claim,
    release_work_claim,
)
from teatree.loop.scanners.issue_intake import IssueIntakeScanner

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

_ISSUE = "https://example.invalid/org/repo/issues/42"
_PR = "https://example.invalid/org/repo/pull/99"
_INTERACTIVE = "interactive-session"
_LOOP = "loop-worker"


class TestBranchClaimedPrRefusesASecondLifecycleClaim(TestCase):
    def _lifecycle_claim(self, *, as_instance: str) -> ImplementedIssueMarker | None:
        """The loop's lifecycle claim of ``_ISSUE``, run as *as_instance*.

        Drives the real scanner seam (``IssueIntakeScanner._claim``) rather than
        the marker manager, so the work-lease gate is exercised where it lives.
        """
        for target, replacement in (
            ("teatree.loop.scanners.issue_intake.instance_id", lambda: as_instance),
            ("teatree.core.fleet.wire.fleet_claim_enabled", lambda _overlay: False),
        ):
            patched = mock.patch(target, replacement)
            patched.start()
            self.addCleanup(patched.stop)
        scanner = IssueIntakeScanner(host=cast("CodeHostBackend", None), admit_label="t3-auto")
        return scanner._claim(_ISSUE)

    def test_the_loop_defers_to_a_live_branch_claim(self) -> None:
        register_work_claim(
            WorkIdentity(repo="org/repo", branch="42-fix", pr_url=_PR, issue_url=_ISSUE), owner=_INTERACTIVE
        )

        claimed = self._lifecycle_claim(as_instance=_LOOP)

        assert claimed is None, "a branch-claimed PR must refuse a second lifecycle claim"
        assert not ImplementedIssueMarker.objects.filter(issue_url=_ISSUE).exists()

    def test_the_same_owner_is_never_blocked_by_its_own_claim(self) -> None:
        register_work_claim(WorkIdentity(repo="org/repo", branch="42-fix", issue_url=_ISSUE), owner=_LOOP)

        assert self._lifecycle_claim(as_instance=_LOOP) is not None

    def test_an_unclaimed_issue_still_claims_normally(self) -> None:
        assert self._lifecycle_claim(as_instance=_LOOP) is not None

    def test_an_expired_claim_releases_the_issue(self) -> None:
        register_work_claim(WorkIdentity(issue_url=_ISSUE), owner=_INTERACTIVE, ttl_seconds=60)
        LoopLease.objects.filter(name=issue_slot(_ISSUE)).update(
            lease_expires_at=timezone.now() - dt.timedelta(minutes=1)
        )

        assert self._lifecycle_claim(as_instance=_LOOP) is not None


class TestWorkSlots:
    """The branch/PR slot builders are the readable, collision-free lease keys."""

    def test_branch_slot_is_stable_and_repo_branch_specific(self) -> None:
        assert branch_slot("org/repo", "42-fix") == branch_slot("org/repo", "42-fix")
        assert branch_slot("org/repo", "42-fix") != branch_slot("org/repo", "43-fix")
        assert branch_slot("org/repo", "42-fix") != branch_slot("other/repo", "42-fix")

    def test_pr_slot_is_stable_and_url_specific(self) -> None:
        assert pr_slot(_PR) == pr_slot(_PR)
        assert pr_slot(_PR) != pr_slot(_PR + "0")

    def test_branch_and_pr_slots_are_distinct_namespaces(self) -> None:
        assert branch_slot("org/repo", "b").startswith("work:branch:")
        assert pr_slot(_PR).startswith("work:pr:")
        assert branch_slot("org/repo", "b") != pr_slot(_PR)


class TestWorkClaimIdentities(TestCase):
    def test_every_known_identity_is_claimed(self) -> None:
        slots = register_work_claim(
            WorkIdentity(repo="org/repo", branch="b", pr_url=_PR, issue_url=_ISSUE), owner=_INTERACTIVE
        )

        assert len(slots) == 3

    def test_re_registering_the_same_owner_is_a_renewal(self) -> None:
        register_work_claim(WorkIdentity(issue_url=_ISSUE), owner=_INTERACTIVE, ttl_seconds=60)
        first = LoopLease.objects.get(name=issue_slot(_ISSUE)).lease_expires_at

        register_work_claim(WorkIdentity(issue_url=_ISSUE), owner=_INTERACTIVE, ttl_seconds=7200)

        assert LoopLease.objects.get(name=issue_slot(_ISSUE)).lease_expires_at > first

    def test_a_rival_owner_wins_nothing(self) -> None:
        register_work_claim(WorkIdentity(issue_url=_ISSUE), owner=_INTERACTIVE)

        assert register_work_claim(WorkIdentity(issue_url=_ISSUE), owner=_LOOP) == []
        assert foreign_work_holder(WorkIdentity(issue_url=_ISSUE), owner=_LOOP) == _INTERACTIVE

    def test_release_frees_the_work_for_another_owner(self) -> None:
        register_work_claim(WorkIdentity(issue_url=_ISSUE, pr_url=_PR), owner=_INTERACTIVE)

        release_work_claim(WorkIdentity(issue_url=_ISSUE, pr_url=_PR), owner=_INTERACTIVE)

        assert foreign_work_holder(WorkIdentity(issue_url=_ISSUE), owner=_LOOP) == ""

    def test_a_non_owner_release_is_a_no_op(self) -> None:
        register_work_claim(WorkIdentity(issue_url=_ISSUE), owner=_INTERACTIVE)

        release_work_claim(WorkIdentity(issue_url=_ISSUE), owner=_LOOP)

        assert foreign_work_holder(WorkIdentity(issue_url=_ISSUE), owner=_LOOP) == _INTERACTIVE
