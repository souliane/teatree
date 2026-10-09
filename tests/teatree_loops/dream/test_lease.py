"""Dead-owner-aware acquisition of the dream pass's in-flight lease (#3993/#4270).

A pass killed mid-run never releases its lease, so the refusal read "another dream
pass is already running" for the rest of a 35-minute TTL while no such process
existed. These prove the reclaim is real (a provably-dead owner is evicted) and
fail-closed (an owner whose liveness cannot be proved keeps it).

Every case pins the reader's pid namespace rather than inheriting the host's, so the
suite is hermetic on a kernel with an unreadable procfs and so a FOREIGN holder can be
minted at all (#4270).
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

#: This reader's pid namespace, and a sibling container's — same shape the kernel
#: publishes at ``/proc/self/ns/pid``, so the tokens under test are the real ones.
HERE = "pid:[4026531000]"
SIBLING_CONTAINER = "pid:[4026532000]"


def _no_pid_probe() -> AbstractContextManager[object]:
    return patch("teatree.core.loop_lease_liveness.pid_alive_probe", return_value=None)


def _reading_from(namespace: str) -> AbstractContextManager[object]:
    """Pin the pid namespace every mint and every liveness read resolves in."""
    return patch("teatree.core.loop_lease_liveness.reader_pid_namespace", return_value=namespace)


class TestLeaseOwnerToken(TestCase):
    def setUp(self) -> None:
        self.enterContext(_reading_from(HERE))

    def test_owner_round_trips_through_its_pid(self) -> None:
        assert lease.owner_pid(lease.lease_owner(4242)) == 4242

    def test_the_token_records_the_namespace_the_pid_resolves_in(self) -> None:
        assert lease.owner_namespace(lease.lease_owner(4242)) == HERE

    def test_a_non_pid_owner_has_no_pid(self) -> None:
        assert lease.owner_pid("some-other-holder") is None

    def test_a_non_pid_owner_is_never_provably_dead(self) -> None:
        assert lease.owner_is_dead("some-other-holder") is False

    def test_an_unprobeable_pid_is_never_provably_dead(self) -> None:
        self.enterContext(_no_pid_probe())
        assert lease.owner_is_dead(lease.lease_owner(DEAD_PID)) is False


class TestForeignNamespaceHolder(TestCase):
    """A pid is namespace-local, so a sibling container's holder is not probeable (#4270)."""

    def test_two_containers_never_mint_the_same_token_for_one_pid(self) -> None:
        # Unqualified, the two tokens are byte-identical, so container B's acquire
        # matches the CAS's own-owner renew arm and takes a live holder's lease
        # without the reclaim path being reached at all.
        with _reading_from(SIBLING_CONTAINER):
            theirs = lease.lease_owner(7)
        with _reading_from(HERE):
            mine = lease.lease_owner(7)

        assert theirs != mine

    def test_a_foreign_holder_is_never_provably_dead(self) -> None:
        with _reading_from(SIBLING_CONTAINER):
            holder = lease.lease_owner(DEAD_PID)

        with _reading_from(HERE):
            assert lease.owner_is_dead(holder) is False

    def test_a_live_foreign_pass_keeps_its_lease(self) -> None:
        with _reading_from(SIBLING_CONTAINER):
            holder = lease.lease_owner(DEAD_PID)
        assert LoopLease.objects.acquire(DREAM_LEASE_NAME, owner=holder)

        with _reading_from(HERE):
            verdict = lease.acquire(owner=lease.lease_owner(os.getpid()), lease_seconds=DREAM_LEASE_SECONDS)

        assert not verdict.acquired
        assert LoopLease.objects.get(name=DREAM_LEASE_NAME).owner == holder

    def test_an_unqualified_legacy_token_is_never_provably_dead(self) -> None:
        # A token minted before the qualification carries no attribution, so nothing
        # can establish its liveness; it lapses on its own TTL instead.
        with _reading_from(HERE):
            assert lease.owner_is_dead(f"pid-{DEAD_PID}") is False


class TestAcquire(TestCase):
    def setUp(self) -> None:
        self.enterContext(_reading_from(HERE))

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
