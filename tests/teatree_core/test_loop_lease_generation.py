"""Fencing / lease-generation token on the t3-master lease (autonomous-lane redesign §5).

The generation is bumped on every CHANGE of holder (a failover reclaim of an
expired lease, or a human take-over steal) and KEPT on a same-holder per-tick
refresh and on a same-process self-reclaim across a compaction session-id
rotation (#2835) — so the master never fences its own in-flight worker.
``token_is_current`` is the git-write fencing check a merge-worker's write
passes only while no newer generation has been granted.
"""

import os
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LoopLease

_SLOT = "t3-master-fence-test"


class TestFencingGenerationBumps(TestCase):
    def test_first_claim_of_empty_slot_bumps_from_zero(self) -> None:
        won, _ = LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        assert won is True
        assert LoopLease.objects.fencing_generation(_SLOT) == 1

    def test_same_session_refresh_keeps_generation(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        gen_after_claim = LoopLease.objects.fencing_generation(_SLOT)
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        assert LoopLease.objects.fencing_generation(_SLOT) == gen_after_claim

    def test_failover_reclaim_of_expired_foreign_lease_bumps(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="dead", owner_pid=None)
        # Expire it with a dead/unknown pid so a different session may reclaim.
        LoopLease.objects.filter(name=_SLOT).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        gen_before = LoopLease.objects.fencing_generation(_SLOT)
        won, _ = LoopLease.objects.claim_ownership(_SLOT, session_id="new", owner_pid=os.getpid())
        assert won is True
        assert LoopLease.objects.fencing_generation(_SLOT) == gen_before + 1

    def test_take_over_by_different_session_bumps(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        gen_before = LoopLease.objects.fencing_generation(_SLOT)
        LoopLease.objects.claim_ownership(_SLOT, session_id="b", owner_pid=os.getpid(), take_over=True)
        assert LoopLease.objects.fencing_generation(_SLOT) == gen_before + 1

    def test_take_over_by_same_session_keeps_generation(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        gen_before = LoopLease.objects.fencing_generation(_SLOT)
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid(), take_over=True)
        assert LoopLease.objects.fencing_generation(_SLOT) == gen_before

    def test_same_process_self_reclaim_across_rotation_keeps_generation(self) -> None:
        # A live foreign session (different id) whose owner_pid is THIS process:
        # context compaction rotated the id but not the process (#2835). The
        # re-anchor to the rotated id is not a transfer, so the generation holds.
        LoopLease.objects.claim_ownership(_SLOT, session_id="old-id", owner_pid=os.getpid())
        gen_before = LoopLease.objects.fencing_generation(_SLOT)
        won, current = LoopLease.objects.claim_ownership(_SLOT, session_id="rotated-id", owner_pid=os.getpid())
        assert won is True
        assert current == "rotated-id"
        assert LoopLease.objects.fencing_generation(_SLOT) == gen_before


class TestFencingTokenCheck(TestCase):
    def test_current_token_passes(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        token = LoopLease.objects.fencing_generation(_SLOT)
        assert LoopLease.objects.token_is_current(_SLOT, token) is True

    def test_stale_token_is_fenced_after_a_steal(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        worker_token = LoopLease.objects.fencing_generation(_SLOT)
        # A human steal bumps the generation; the worker's stamped token is now stale.
        LoopLease.objects.claim_ownership(_SLOT, session_id="human", owner_pid=os.getpid(), take_over=True)
        assert LoopLease.objects.token_is_current(_SLOT, worker_token) is False

    def test_missing_row_reports_zero_and_admits_zero_token(self) -> None:
        assert LoopLease.objects.fencing_generation("never-claimed") == 0
        assert LoopLease.objects.token_is_current("never-claimed", 0) is True

    def test_ownership_status_surfaces_generation(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        status = LoopLease.objects.ownership_status(_SLOT)
        assert status.generation == LoopLease.objects.fencing_generation(_SLOT)
        assert status.generation >= 1

    def test_ownership_status_of_missing_slot_is_generation_zero(self) -> None:
        status = LoopLease.objects.ownership_status("never-claimed")
        assert status.generation == 0
