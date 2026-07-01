"""t3-master collision guardrails for the inert maker-only pane layer (#1838 PR#7a).

Safety-critical. A team-role pane claims ONLY its ``team:<role>`` slot and can
NEVER claim the global ``t3-master`` slot or a ``loop:<name>`` per-loop slot —
the proven disjoint key spaces are the backbone. The symmetric must/must-not
test pins both directions. The pre-work live-owner check lets a pane skip (the
#744 zeroed-contract path) while another session's live ``t3-master`` is driving.
"""

import os

import pytest
from django.test import TestCase

from teatree.core.loop_lease_manager import T3_MASTER_SLOT, per_loop_owner_slot
from teatree.teams.guardrails import LoopOwnerCollisionError, assert_pane_claim_allowed, live_owner_blocks_pane
from teatree.teams.roles import TeamRole, team_claim_slot


class TestPaneClaimNamespaceGuard:
    """A pane may claim ``team:<role>``; it must never claim a t3-master slot."""

    def test_team_role_slot_is_allowed(self) -> None:
        for role in TeamRole:
            # Must NOT raise — a team-role slot is the only thing a pane may claim.
            assert_pane_claim_allowed(team_claim_slot(role))

    def test_bare_role_is_qualified_then_allowed(self) -> None:
        # A bare role slug is qualified UP to ``team:<role>`` and allowed.
        assert_pane_claim_allowed(team_claim_slot("core-maker"))

    def test_global_owner_slot_is_rejected(self) -> None:
        with pytest.raises(LoopOwnerCollisionError):
            assert_pane_claim_allowed(T3_MASTER_SLOT)

    def test_per_loop_owner_slot_is_rejected(self) -> None:
        with pytest.raises(LoopOwnerCollisionError):
            assert_pane_claim_allowed(per_loop_owner_slot("dispatch"))

    def test_bare_loop_owner_literal_is_rejected(self) -> None:
        with pytest.raises(LoopOwnerCollisionError):
            assert_pane_claim_allowed("t3-master")

    def test_loop_prefixed_slot_is_rejected(self) -> None:
        with pytest.raises(LoopOwnerCollisionError):
            assert_pane_claim_allowed("loop:work")

    def test_unknown_non_team_slot_is_rejected(self) -> None:
        # Fail-closed: anything not provably a ``team:<role>`` slot is rejected,
        # so a pane can never claim an infra lease (``loop-tick`` etc.) either.
        with pytest.raises(LoopOwnerCollisionError):
            assert_pane_claim_allowed("loop-tick")
        with pytest.raises(LoopOwnerCollisionError):
            assert_pane_claim_allowed("")


class TestPreWorkLiveOwnerCheck(TestCase):
    """A pane skips (zeroed contract) when a live foreign ``t3-master`` exists."""

    def test_live_foreign_owner_blocks_pane(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # A different live session owns the loop (alive pid).
        LoopLease.objects.claim_ownership(
            T3_MASTER_SLOT, session_id="live-lead", owner_pid=os.getpid(), ttl_seconds=1800
        )
        assert live_owner_blocks_pane(pane_session_id="pane-1") is True

    def test_no_owner_does_not_block(self) -> None:
        assert live_owner_blocks_pane(pane_session_id="pane-1") is False

    def test_own_session_does_not_block(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # The owner IS this pane's session — not a foreign owner, so no skip.
        LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id="pane-1", owner_pid=os.getpid(), ttl_seconds=1800)
        assert live_owner_blocks_pane(pane_session_id="pane-1") is False

    def test_dead_owner_does_not_block(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # An expired, dead-pid owner is reclaimable — not a live owner, no skip.
        LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id="gone", owner_pid=2_999_999, ttl_seconds=-1)
        assert live_owner_blocks_pane(pane_session_id="pane-1") is False
