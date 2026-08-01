"""Dead-owner-aware acquisition of the dream pass's in-flight lease (#3993).

A pass killed mid-run never releases its lease, so the refusal read "another dream
pass is already running" for the rest of a 35-minute TTL while no such process
existed. These prove the reclaim is real (a provably-dead owner is evicted) and
fail-closed (an owner whose liveness cannot be proved keeps it).
"""

import os
from contextlib import AbstractContextManager
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import LoopLease
from teatree.loops.dream import lease
from teatree.loops.dream.loop import DREAM_LEASE_NAME, DREAM_LEASE_SECONDS

#: Above the kernel's maximum pid, so no process can ever hold it.
DEAD_PID = 2**22 + 7


def _no_pid_probe() -> AbstractContextManager[object]:
    return patch("teatree.core.loop_lease_liveness.pid_alive_probe", return_value=None)


class TestLeaseOwnerToken(TestCase):
    def test_owner_round_trips_through_its_pid(self) -> None:
        assert lease.owner_pid(lease.lease_owner(4242)) == 4242

    def test_a_non_pid_owner_has_no_pid(self) -> None:
        assert lease.owner_pid("some-other-holder") is None

    def test_a_non_pid_owner_is_never_provably_dead(self) -> None:
        assert lease.owner_is_dead("some-other-holder") is False

    def test_an_unprobeable_pid_is_never_provably_dead(self) -> None:
        self.enterContext(_no_pid_probe())
        assert lease.owner_is_dead(lease.lease_owner(DEAD_PID)) is False


class TestAcquire(TestCase):
    def test_acquires_an_unheld_lease(self) -> None:
        verdict = lease.acquire(owner=lease.lease_owner(1), lease_seconds=DREAM_LEASE_SECONDS)
        assert verdict.acquired
        assert verdict.message == ""

    def test_a_live_holder_still_blocks_and_is_named_as_live(self) -> None:
        alive = lease.lease_owner(os.getpid())
        assert LoopLease.objects.acquire(DREAM_LEASE_NAME, owner=alive)

        verdict = lease.acquire(owner=lease.lease_owner(DEAD_PID), lease_seconds=DREAM_LEASE_SECONDS)

        assert not verdict.acquired
        assert "LIVE" in verdict.message
        assert alive in verdict.message

    def test_a_dead_holder_is_reclaimed_so_a_manual_run_is_not_blocked(self) -> None:
        assert LoopLease.objects.acquire(DREAM_LEASE_NAME, owner=lease.lease_owner(DEAD_PID))
        mine = lease.lease_owner(os.getpid())

        verdict = lease.acquire(owner=mine, lease_seconds=DREAM_LEASE_SECONDS)

        assert verdict.acquired
        assert "dead" in verdict.message
        assert LoopLease.objects.get(name=DREAM_LEASE_NAME).owner == mine

    def test_an_unprobeable_holder_keeps_the_lease(self) -> None:
        # Fail-closed: without a liveness probe nothing is PROVABLY dead, so the
        # reclaim must not fire on a guess.
        assert LoopLease.objects.acquire(DREAM_LEASE_NAME, owner=lease.lease_owner(DEAD_PID))
        self.enterContext(_no_pid_probe())

        verdict = lease.acquire(owner=lease.lease_owner(os.getpid()), lease_seconds=DREAM_LEASE_SECONDS)

        assert not verdict.acquired
