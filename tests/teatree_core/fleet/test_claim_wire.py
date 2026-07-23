"""Wiring of the Stage 2 mutex into the issue-implementer dispatch + the ship fence.

The kill-switch ``fleet_claim_enabled`` is default-OFF, so with it off the
scanner's claim is byte-for-byte today's local-only ``ImplementedIssueMarker.claim``.
With it on, the GitHub claim ref becomes the AUTHORITY: the marker is a cache
stamped with the fencing sha (via ``cache_from_fleet_claim``), and the ship fence
(:func:`run_fleet_claim_fence_gate`) refuses to open the PR for a claim this
instance no longer holds. All against a real local bare-git origin.

The fleet acquire is wired at the dispatch layer (``IssueIntakeScanner``), not
in the model manager, so ``teatree.core.models`` keeps no dependency on the higher
``teatree.core`` coordination modules (the tach boundary).
"""

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from django.test import TestCase

from teatree.core.fleet import claim as fleet_claim
from teatree.core.fleet import wire as fleet_claim_wire
from teatree.core.management.commands._ship.gates import run_fleet_claim_fence_gate
from teatree.core.models import ImplementedIssueMarker, Ticket, Worktree
from teatree.loop.scanners.issue_intake import IssueIntakeScanner

from ._git_origin import init_bare, init_client, ref_sha

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

_ISSUE = "https://github.com/souliane/teatree/issues/4242"


def _scanner() -> IssueIntakeScanner:
    return IssueIntakeScanner(host=cast("CodeHostBackend", object()), admit_label="t3-auto", overlay_name="acme")


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


class TestScannerHeartbeatOnly(TestCase):
    """At full budget (``can_claim=False``) the scanner heartbeats but claims nothing."""

    def test_scan_heartbeats_and_claims_nothing(self) -> None:
        scanner = IssueIntakeScanner(
            host=cast("CodeHostBackend", object()), admit_label="t3-auto", overlay_name="acme", can_claim=False
        )
        with patch.object(fleet_claim_wire, "heartbeat_inflight_claims") as beat:
            signals = scanner.scan()
        assert signals == []
        beat.assert_called_once_with("acme")


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
        ticket = Ticket.objects.create(overlay="acme", issue_url=_ISSUE, state=Ticket.State.REVIEWED)
        worktree = Worktree.objects.create(
            ticket=ticket, overlay="acme", repo_path=str(holder), branch="feat", extra={"worktree_path": str(holder)}
        )
        # Build the marker through the REAL production path — the manager NEVER sets
        # the ticket FK, so the fence must resolve it by the marker's (issue_url,
        # overlay) natural key, exactly as it will in production.
        ImplementedIssueMarker.objects.cache_from_fleet_claim(
            _ISSUE, "acme", claim_ref_sha=claim.sha, claimed_by_instance=claim.instance_id
        )
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
        assert "no longer held by this instance" in failure["error"]

    def test_ticket_with_issue_url_but_no_marker_is_a_no_op(self) -> None:
        tmp = self._tmp()
        init_bare(tmp / "origin.git")
        ticket = Ticket.objects.create(overlay="acme", issue_url=_ISSUE, state=Ticket.State.REVIEWED)
        worktree = Worktree.objects.create(
            ticket=ticket, overlay="acme", repo_path=str(tmp), branch="feat", extra={"worktree_path": str(tmp)}
        )
        with patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True):
            assert run_fleet_claim_fence_gate(ticket, worktree) is None

    def test_ticket_without_issue_url_is_a_no_op(self) -> None:
        # A ticket with no issue_url cannot carry a fleet claim to fence — short-circuit.
        tmp = self._tmp()
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.REVIEWED)
        worktree = Worktree.objects.create(
            ticket=ticket, overlay="acme", repo_path=str(tmp), branch="feat", extra={"worktree_path": str(tmp)}
        )
        with patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True):
            assert run_fleet_claim_fence_gate(ticket, worktree) is None

    def test_fence_fails_closed_when_ref_infra_unreachable(self) -> None:
        tmp = self._tmp()
        ticket = Ticket.objects.create(overlay="acme", issue_url=_ISSUE, state=Ticket.State.REVIEWED)
        ImplementedIssueMarker.objects.cache_from_fleet_claim(
            _ISSUE, "acme", claim_ref_sha="a" * 40, claimed_by_instance="box-holder"
        )
        broken = init_client(tmp / "broken", tmp / "absent.git")  # origin absent -> ls-remote raises
        with patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True):
            assert fleet_claim_wire.ticket_claim_is_lost(ticket, str(broken)) is True


def _tempdir(tc: TestCase) -> Path:
    import tempfile  # noqa: PLC0415 — test-local

    return Path(tc.enterContext(tempfile.TemporaryDirectory()))


class TestHeartbeatSweep(TestCase):
    """`heartbeat_inflight_claims` keeps in-flight claims un-stealable (Stage 2, B1)."""

    def test_heartbeat_keeps_a_live_claim_un_stealable(self) -> None:
        tmp = _tempdir(self)
        bare = init_bare(tmp / "o.git")
        holder = init_client(tmp / "holder", bare)
        thief = init_client(tmp / "thief", bare)
        # Holder claims at t=1000 with a SHORT ttl -> the ORIGINAL claim expires at
        # t=1100. Without the heartbeat a steal at t=2000 would succeed (RED).
        claim = fleet_claim.acquire(_ISSUE, repo=str(holder), remote="origin", ttl_seconds=100.0, now=1000.0)
        assert claim is not None
        marker = ImplementedIssueMarker.objects.create(issue_url=_ISSUE, overlay="acme", claim_ref_sha=claim.sha)
        with (
            patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True),
            patch.object(fleet_claim_wire, "resolve_claim_repo", lambda _: str(holder)),
            patch("teatree.core.fleet.claim.time.time", return_value=1090.0),
        ):
            fleet_claim_wire.heartbeat_inflight_claims("acme")
        # The heartbeat re-affirmed the claim at t=1090 with the full (4h) TTL, so a
        # steal at t=2000 now finds it LIVE and stands down — GREEN only with B1.
        assert fleet_claim.steal_if_expired(_ISSUE, repo=str(thief), remote="origin", now=2000.0) is None
        marker.refresh_from_db()
        assert marker.claim_ref_sha != claim.sha  # the ref was re-pointed
        assert marker.state != ImplementedIssueMarker.State.ABANDONED

    def test_heartbeat_abandons_a_stolen_claim(self) -> None:
        tmp = _tempdir(self)
        bare = init_bare(tmp / "o.git")
        holder = init_client(tmp / "holder", bare)
        thief = init_client(tmp / "thief", bare)
        claim = fleet_claim.acquire(_ISSUE, repo=str(holder), remote="origin", ttl_seconds=10.0, now=1000.0)
        assert claim is not None
        marker = ImplementedIssueMarker.objects.create(issue_url=_ISSUE, overlay="acme", claim_ref_sha=claim.sha)
        # A rival steals the expired claim: the ref moves off the holder's sha.
        assert fleet_claim.steal_if_expired(_ISSUE, repo=str(thief), remote="origin", ttl_seconds=10.0, now=5000.0)
        with (
            patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True),
            patch.object(fleet_claim_wire, "resolve_claim_repo", lambda _: str(holder)),
        ):
            fleet_claim_wire.heartbeat_inflight_claims("acme")
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.ABANDONED

    def test_heartbeat_is_a_no_op_when_switch_off(self) -> None:
        marker = ImplementedIssueMarker.objects.create(issue_url=_ISSUE, overlay="acme", claim_ref_sha="a" * 40)
        with patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=False):
            fleet_claim_wire.heartbeat_inflight_claims("acme")
        marker.refresh_from_db()
        assert marker.claim_ref_sha == "a" * 40
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED

    def test_heartbeat_skips_a_marker_when_no_repo_resolves(self) -> None:
        marker = ImplementedIssueMarker.objects.create(issue_url=_ISSUE, overlay="acme", claim_ref_sha="a" * 40)
        with (
            patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True),
            patch.object(fleet_claim_wire, "resolve_claim_repo", lambda _: ""),
        ):
            fleet_claim_wire.heartbeat_inflight_claims("acme")
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED  # untouched, not abandoned

    def test_heartbeat_leaves_claim_when_infra_unreachable(self) -> None:
        tmp = _tempdir(self)
        client = init_client(tmp / "c", tmp / "absent.git")  # valid local repo, absent origin
        marker = ImplementedIssueMarker.objects.create(issue_url=_ISSUE, overlay="acme", claim_ref_sha="a" * 40)
        with (
            patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True),
            patch.object(fleet_claim_wire, "resolve_claim_repo", lambda _: str(client)),
        ):
            fleet_claim_wire.heartbeat_inflight_claims("acme")  # transient: leave for retry, never abandon
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED
        assert marker.claim_ref_sha == "a" * 40


class TestExecuteShipFence(TestCase):
    """B2: ``execute_ship`` re-fences and aborts (no push, no PR) under a stolen claim."""

    def test_push_and_open_aborts_without_pushing_when_claim_stolen(self) -> None:
        tmp = _tempdir(self)
        bare = init_bare(tmp / "o.git")
        holder = init_client(tmp / "holder", bare)
        thief = init_client(tmp / "thief", bare)
        claim = fleet_claim.acquire(_ISSUE, repo=str(holder), remote="origin")
        assert claim is not None
        ticket = Ticket.objects.create(overlay="acme", issue_url=_ISSUE, state=Ticket.State.SHIPPED)
        ImplementedIssueMarker.objects.cache_from_fleet_claim(
            _ISSUE, "acme", claim_ref_sha=claim.sha, claimed_by_instance=claim.instance_id
        )
        assert fleet_claim.steal_if_expired(_ISSUE, repo=str(thief), remote="origin", now=1e12) is not None

        from teatree.core.runners.ship import ShipExecutor  # noqa: PLC0415 — test-local

        pushed: list[dict] = []
        executor = ShipExecutor(ticket)
        with (
            patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True),
            patch.object(ShipExecutor, "_check_branch_currency", return_value=None),
            patch("teatree.core.runners.ship.git.push", side_effect=lambda **kw: pushed.append(kw)),
        ):
            result = executor._push_and_open(ticket, {}, cast("object", None), str(holder), "feat")

        assert result.ok is False
        assert "no longer held by this instance" in result.detail
        assert pushed == []  # fenced CLOSED before the push — never pushed under a lost claim

    def test_re_fences_between_the_push_and_the_pr_open(self) -> None:
        # The push and the PR-open are two distinct outward writes; a claim stolen in
        # the gap between them must abort the ship BEFORE the create.
        tmp = _tempdir(self)
        bare = init_bare(tmp / "o.git")
        holder = init_client(tmp / "holder", bare)
        thief = init_client(tmp / "thief", bare)
        claim = fleet_claim.acquire(_ISSUE, repo=str(holder), remote="origin")
        assert claim is not None
        ticket = Ticket.objects.create(overlay="acme", issue_url=_ISSUE, state=Ticket.State.SHIPPED)
        ImplementedIssueMarker.objects.cache_from_fleet_claim(
            _ISSUE, "acme", claim_ref_sha=claim.sha, claimed_by_instance=claim.instance_id
        )

        from teatree.core.runners.ship import ShipExecutor  # noqa: PLC0415 — test-local

        def _steal_during_push(**_kw: object) -> None:
            # a rival steals the claim in the gap between the push and the PR-open
            fleet_claim.steal_if_expired(_ISSUE, repo=str(thief), remote="origin", now=1e12)

        executor = ShipExecutor(ticket)
        with (
            patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True),
            patch.object(ShipExecutor, "_check_branch_currency", return_value=None),
            patch("teatree.core.runners.ship.git.push", side_effect=_steal_during_push),
        ):
            result = executor._push_and_open(ticket, {}, cast("object", None), str(holder), "feat")

        assert result.ok is False
        assert "no longer held by this instance" in result.detail


class TestEnsurePrFence(TestCase):
    """B3: the orphan-branch PR-create is fenced under a stolen claim."""

    def test_orphan_pr_gate_blocks_under_a_lost_claim(self) -> None:
        tmp = _tempdir(self)
        bare = init_bare(tmp / "o.git")
        holder = init_client(tmp / "holder", bare)
        thief = init_client(tmp / "thief", bare)
        claim = fleet_claim.acquire(_ISSUE, repo=str(holder), remote="origin")
        assert claim is not None
        ticket = Ticket.objects.create(overlay="acme", issue_url=_ISSUE, state=Ticket.State.REVIEWED)
        ImplementedIssueMarker.objects.cache_from_fleet_claim(
            _ISSUE, "acme", claim_ref_sha=claim.sha, claimed_by_instance=claim.instance_id
        )
        assert fleet_claim.steal_if_expired(_ISSUE, repo=str(thief), remote="origin", now=1e12) is not None

        from teatree.core.management.commands._ensure_pr import (  # noqa: PLC0415 — test-local
            _owning_ticket_pre_create_gate,
        )

        with (
            patch.object(fleet_claim_wire, "fleet_claim_enabled", return_value=True),
            patch("teatree.core.management.commands._ensure_pr.check_pr_budget"),
            patch("teatree.core.management.commands._ensure_pr.evaluate_debt_delta", return_value=None),
        ):
            result = _owning_ticket_pre_create_gate(
                ticket, "souliane/teatree", str(holder), "feat", cast("object", None)
            )

        assert result is not None
        assert "refusing to open a PR" in result["error"]
