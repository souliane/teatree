"""Fencing / lease-generation token on the t3-master lease (autonomous-lane redesign §5).

The generation is bumped on every CHANGE of holder (a failover reclaim of an
expired lease, or a human take-over steal) and KEPT on a same-holder per-tick
refresh and on a same-process self-reclaim across a compaction session-id
rotation (#2835) — so the master never fences its own in-flight worker.
The ownership status exposes the generation for observation.
"""

import os
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LoopLease

_SLOT = "t3-master-fence-test"


def _stored_generation() -> int:
    return LoopLease.objects.get(name=_SLOT).generation


class TestFencingGenerationBumps(TestCase):
    def test_first_claim_of_empty_slot_bumps_from_zero(self) -> None:
        won, _ = LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        assert won is True
        assert _stored_generation() == 1

    def test_same_session_refresh_keeps_generation(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        gen_after_claim = _stored_generation()
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        assert _stored_generation() == gen_after_claim

    def test_failover_reclaim_of_expired_foreign_lease_bumps(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="dead", owner_pid=None)
        # Expire it with a dead/unknown pid so a different session may reclaim.
        LoopLease.objects.filter(name=_SLOT).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        gen_before = _stored_generation()
        won, _ = LoopLease.objects.claim_ownership(_SLOT, session_id="new", owner_pid=os.getpid())
        assert won is True
        assert _stored_generation() == gen_before + 1

    def test_take_over_by_different_session_bumps(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        gen_before = _stored_generation()
        LoopLease.objects.take_over_ownership(_SLOT, session_id="b", owner_pid=os.getpid())
        assert _stored_generation() == gen_before + 1

    def test_take_over_by_same_session_keeps_generation(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        gen_before = _stored_generation()
        LoopLease.objects.take_over_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        assert _stored_generation() == gen_before

    def test_same_process_self_reclaim_across_rotation_keeps_generation(self) -> None:
        # A live foreign session (different id) whose owner_pid is THIS process:
        # context compaction rotated the id but not the process (#2835). The
        # re-anchor to the rotated id is not a transfer, so the generation holds.
        LoopLease.objects.claim_ownership(_SLOT, session_id="old-id", owner_pid=os.getpid())
        gen_before = _stored_generation()
        won, current = LoopLease.objects.claim_ownership(_SLOT, session_id="rotated-id", owner_pid=os.getpid())
        assert won is True
        assert current == "rotated-id"
        assert _stored_generation() == gen_before


class TestFencingTokenCheck(TestCase):
    def test_ownership_status_surfaces_generation(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        status = LoopLease.objects.ownership_status(_SLOT)
        assert status.generation == _stored_generation()
        assert status.generation >= 1

    def test_ownership_status_of_missing_slot_is_generation_zero(self) -> None:
        status = LoopLease.objects.ownership_status("never-claimed")
        assert status.generation == 0
