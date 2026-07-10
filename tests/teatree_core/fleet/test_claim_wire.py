"""Wiring of the Stage 2 mutex into the issue-implementer dispatch + the ship fence.

The kill-switch ``fleet_claim_enabled`` is default-OFF, so with it off the
scanner's claim is byte-for-byte today's local-only ``ImplementedIssueMarker.claim``.
With it on, the GitHub claim ref becomes the AUTHORITY: the marker is a cache
stamped with the fencing sha (via ``cache_from_fleet_claim``), and the ship fence
(:func:`run_fleet_claim_fence_gate`) refuses to open the PR for a claim this
instance no longer holds. All against a real local bare-git origin.

The fleet acquire is wired at the dispatch layer (``IssueImplementerScanner``), not
in the model manager, so ``teatree.core.models`` keeps no dependency on the higher
``teatree.core`` coordination modules (the tach boundary).
"""

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from django.test import TestCase

from teatree.core import fleet_claim, fleet_claim_wire
from teatree.core.management.commands._ship.gates import run_fleet_claim_fence_gate
from teatree.core.models import ImplementedIssueMarker, Ticket, Worktree
from teatree.loop.scanners.issue_implementer import IssueImplementerScanner

from ._git_origin import init_bare, init_client, ref_sha

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

_ISSUE = "https://github.com/souliane/teatree/issues/4242"


def _scanner() -> IssueImplementerScanner:
    return IssueImplementerScanner(host=cast("CodeHostBackend", object()), label="impl", overlay_name="acme")


def _enable_and_route(client: Path) -> tuple:
    """Turn the kill-switch on and route claim pushes at *client* (its origin = the test bare)."""
    return (
        patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True),
        patch.object(fleet_claim_wire, "resolve_claim_repo", return_value=str(client)),
    )


class TestDispatchClaimFlagOff(TestCase):
    """Kill-switch OFF (default): the ref infra is never touched."""

    def test_claim_is_local_only_get_or_create(self) -> None:
        first = _scanner()._claim(_ISSUE)
        again = _scanner()._claim(_ISSUE)
        assert first is not None
        assert first.claim_ref_sha == ""  # no ref taken when the switch is off
        assert again is None  # local get_or_create dedup, exactly today's behaviour


class TestDispatchClaimFlagOn(TestCase):
    def test_acquires_ref_and_stamps_fencing_sha(self) -> None:
        tmp = Path(self._make_tmp())
        bare = init_bare(tmp / "origin.git")
        client = init_client(tmp / "client", bare)
        enable, route = _enable_and_route(client)
        with enable, route:
            row = _scanner()._claim(_ISSUE)

        assert row is not None
        ref = fleet_claim.claim_ref(_ISSUE)
        assert row.claim_ref_sha == ref_sha(bare, ref) != ""  # cache stamped with the live ref sha

    def test_second_claim_finds_ref_held_and_stands_down(self) -> None:
        tmp = Path(self._make_tmp())
        bare = init_bare(tmp / "origin.git")
        client = init_client(tmp / "client", bare)
        enable, route = _enable_and_route(client)
        with enable, route:
            first = _scanner()._claim(_ISSUE)
            # A re-tick while the claim is live: the ref exists, so no new claim
            # is granted (the loop skips — exactly-once across ticks).
            second = _scanner()._claim(_ISSUE)
        assert first is not None
        assert second is None

    def test_unreachable_ref_infra_fails_safe_no_row(self) -> None:
        tmp = Path(self._make_tmp())
        # A client whose origin points at a bare repo that does not exist: every
        # remote op errors -> FleetClaimUnavailableError -> the wire fails safe.
        client = init_client(tmp / "client", tmp / "does-not-exist.git")
        with (
            patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True),
            patch.object(fleet_claim_wire, "resolve_claim_repo", return_value=str(client)),
        ):
            row = _scanner()._claim(_ISSUE)

        assert row is None  # failed safe: did not claim
        assert not ImplementedIssueMarker.objects.filter(issue_url=_ISSUE).exists()

    def _make_tmp(self) -> str:
        import tempfile  # noqa: PLC0415 — test-local

        return self.enterContext(tempfile.TemporaryDirectory())


class TestCacheFromFleetClaim(TestCase):
    """The marker is a CACHE of the ref: create, then reconcile to a new token."""

    def test_creates_then_updates_in_place(self) -> None:
        first = ImplementedIssueMarker.objects.cache_from_fleet_claim(
            _ISSUE, "acme", claim_ref_sha="a" * 40, claimed_by_instance="box-1"
        )
        # A reclaim (steal) re-stamps the SAME cache row with the new fencing token.
        second = ImplementedIssueMarker.objects.cache_from_fleet_claim(
            _ISSUE, "acme", claim_ref_sha="b" * 40, claimed_by_instance="box-2"
        )
        assert first.pk == second.pk
        second.refresh_from_db()
        assert second.claim_ref_sha == "b" * 40
        assert second.claimed_by_instance == "box-2"


class TestShipFenceGate(TestCase):
    """``run_fleet_claim_fence_gate`` — the outward-write fence at ``pr create``."""

    def _tmp(self) -> Path:
        import tempfile  # noqa: PLC0415 — test-local

        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _ship_setup(self, tmp: Path) -> tuple[Ticket, Worktree, str, Path]:
        bare = init_bare(tmp / "origin.git")
        holder = init_client(tmp / "holder", bare)
        claim = fleet_claim.acquire(_ISSUE, repo=str(holder), remote="origin")
        assert claim is not None
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.REVIEWED)
        worktree = Worktree.objects.create(
            ticket=ticket, overlay="acme", repo_path=str(holder), branch="feat", extra={"worktree_path": str(holder)}
        )
        ImplementedIssueMarker.objects.create(issue_url=_ISSUE, overlay="acme", ticket=ticket, claim_ref_sha=claim.sha)
        return ticket, worktree, claim.sha, bare

    def test_off_switch_is_a_no_op(self) -> None:
        tmp = self._tmp()
        ticket, worktree, _sha, _bare = self._ship_setup(tmp)
        with patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=False):
            assert run_fleet_claim_fence_gate(ticket, worktree) is None

    def test_passes_when_claim_still_held(self) -> None:
        tmp = self._tmp()
        ticket, worktree, _sha, _bare = self._ship_setup(tmp)
        with patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True):
            assert run_fleet_claim_fence_gate(ticket, worktree) is None

    def test_blocks_when_claim_was_stolen(self) -> None:
        tmp = self._tmp()
        ticket, worktree, _sha, bare = self._ship_setup(tmp)
        # Another instance steals the expired claim, moving the ref off our sha.
        thief = init_client(tmp / "thief", bare)
        stolen = fleet_claim.steal_if_expired(_ISSUE, repo=str(thief), remote="origin", now=1e12)
        assert stolen is not None
        with patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True):
            failure = run_fleet_claim_fence_gate(ticket, worktree)
        assert failure is not None
        assert failure["allowed"] is False
        assert _ISSUE in failure["error"]

    def test_ticket_without_fleet_claim_is_a_no_op(self) -> None:
        tmp = self._tmp()
        init_bare(tmp / "origin.git")
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.REVIEWED)
        worktree = Worktree.objects.create(
            ticket=ticket, overlay="acme", repo_path=str(tmp), branch="feat", extra={"worktree_path": str(tmp)}
        )
        with patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True):
            assert run_fleet_claim_fence_gate(ticket, worktree) is None
