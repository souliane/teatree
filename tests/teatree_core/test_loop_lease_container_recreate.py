"""A container recreate must not strand the ``t3-master`` lease (#4253).

The measured outage: the worker held ``t3-master`` under its in-container pid, every
owner-gated ``t3`` cycle ran from a SIBLING container where that number named nothing,
and ``pid_alive`` answered "provably dead" about a process it could not see — so the gate
reported the slot unheld for an hour while the worker drove every tick, and the only
thing that cleared it was the next stack recreate.

A pid is namespace-local, so both directions of the misreading are pinned here: an absent
number must not prove death, and a COLLIDING number must not prove life (nor stand in for
"this is our own process", which would let a sibling container re-anchor or evict the live
worker's lease). The write side records the claimer's namespace; the read side is patched
to a different one, which is exactly the two-container shape the deployment produces.
"""

import datetime as dt
from unittest.mock import patch

import pytest
from django.utils import timezone

import teatree.core.loop_lease_liveness as liveness
import teatree.core.loop_lease_manager as manager
from teatree.core.gates.t3_master_gate import T3MasterGate, t3_master_verdict
from teatree.core.loop_lease_manager import T3_MASTER_SLOT
from teatree.core.models import LoopLease
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

#: The worker container's pid namespace, and a sibling one running the gated `t3` cycles.
WORKER_NS = "pid:[4026532790]"
SIBLING_NS = "pid:[4026532619]"

#: The worker's in-container pid. Low numbers like this are exactly the ones a sibling
#: container also has, which is why a bare integer decides nothing across a namespace.
WORKER_PID = 7


def _claim_as_worker(session_id: str = LOOP_RUNNER_SESSION_ID, *, ttl_seconds: int = 1800) -> None:
    """Claim ``t3-master`` the way the worker does: its own pid, stamped with its namespace."""
    with patch.object(manager, "reader_pid_namespace", return_value=WORKER_NS):
        LoopLease.objects.claim_ownership(
            T3_MASTER_SLOT, session_id=session_id, owner_pid=WORKER_PID, ttl_seconds=ttl_seconds
        )


def _read_from_sibling(*, pid_alive: bool):
    """Read the lease as a sibling container: another namespace, its own answer for the pid."""
    return (
        patch.object(liveness, "reader_pid_namespace", return_value=SIBLING_NS),
        patch("teatree.utils.singleton.pid_alive", return_value=pid_alive),
    )


class TestASiblingContainerReadsTheWorkersLease:
    """The measured symptom: the live worker's own lease reading as unheld from next door."""

    def test_an_absent_pid_in_another_namespace_does_not_prove_the_owner_dead(self) -> None:
        _claim_as_worker()

        namespace, probe = _read_from_sibling(pid_alive=False)
        with namespace, probe:
            assert LoopLease.objects.ownership_status(T3_MASTER_SLOT).is_live is True

    def test_the_gate_lets_an_owner_gated_cycle_run(self) -> None:
        # End to end: this is the SKIP that took two loops dark for an hour.
        _claim_as_worker()

        namespace, probe = _read_from_sibling(pid_alive=False)
        with namespace, probe:
            assert t3_master_verdict(caller_session="sibling").outcome is T3MasterGate.RUN

    def test_the_lease_still_lapses_when_the_worker_stops_refreshing(self) -> None:
        # The TTL is the release: an unattributable pid buys no protection past it, so a
        # genuinely dead worker's slot does not stay pinned forever.
        _claim_as_worker(ttl_seconds=1)
        LoopLease.objects.filter(name=T3_MASTER_SLOT).update(lease_expires_at=timezone.now() - dt.timedelta(seconds=5))

        namespace, probe = _read_from_sibling(pid_alive=True)
        with namespace, probe:
            assert LoopLease.objects.ownership_status(T3_MASTER_SLOT).is_live is False


class TestASiblingContainerDoesNotEvictTheWorkersLease:
    """The mirror hazard: a bare integer match standing in for "our own process"."""

    def test_a_sweep_from_another_namespace_leaves_a_live_lease_alone(self) -> None:
        _claim_as_worker()

        namespace, probe = _read_from_sibling(pid_alive=False)
        with namespace, probe:
            reclaimed = LoopLease.objects.reclaim_dead_owner_leases(current_pid=WORKER_PID + 1)

        assert T3_MASTER_SLOT not in reclaimed
        assert LoopLease.objects.get(name=T3_MASTER_SLOT).session_id == LOOP_RUNNER_SESSION_ID

    def test_a_colliding_pid_is_not_read_as_this_processs_own_lease(self) -> None:
        _claim_as_worker()

        namespace, probe = _read_from_sibling(pid_alive=True)
        with namespace, probe:
            orphaned = LoopLease.objects.evict_stale_owner(
                T3_MASTER_SLOT, keep_session_id="sibling", current_pid=WORKER_PID
            )

        assert orphaned == 0
        assert LoopLease.objects.get(name=T3_MASTER_SLOT).session_id == LOOP_RUNNER_SESSION_ID


class TestTheNextWorkerGenerationRecovers:
    """Kill the holder, recreate the container: the slot comes back under a fresh token."""

    def _worker_generation_one_is_gone(self) -> None:
        _claim_as_worker(session_id="worker-gen1")

    def test_a_colliding_pid_never_hands_a_new_container_the_old_fencing_token(self) -> None:
        # A same-process self-reclaim preserves the §5 generation because the process did
        # not change. A DIFFERENT container reusing the number is a genuine transfer, and
        # keeping the token there would leave an already-fenced generation un-fenced.
        self._worker_generation_one_is_gone()
        LoopLease.objects.filter(name=T3_MASTER_SLOT).update(lease_expires_at=timezone.now() - dt.timedelta(seconds=5))
        before = LoopLease.objects.get(name=T3_MASTER_SLOT).generation

        with (
            patch.object(liveness, "reader_pid_namespace", return_value=SIBLING_NS),
            patch.object(manager, "reader_pid_namespace", return_value=SIBLING_NS),
            patch("teatree.utils.singleton.pid_alive", return_value=True),
        ):
            won, owner = LoopLease.objects.claim_ownership(
                T3_MASTER_SLOT, session_id="worker-gen2", owner_pid=WORKER_PID
            )

        assert (won, owner) == (True, "worker-gen2")
        row = LoopLease.objects.get(name=T3_MASTER_SLOT)
        assert row.generation > before, "a transfer between containers must bump the fencing token"
        assert row.owner_pid_namespace == SIBLING_NS, "the new holder's namespace must replace the dead one's"

    def test_a_live_foreign_container_is_not_hijacked_before_its_ttl(self) -> None:
        # Recovery is via the TTL, not by a guess: while the old lease is still inside its
        # TTL a different container must not take it, however its pids happen to line up.
        self._worker_generation_one_is_gone()

        with (
            patch.object(liveness, "reader_pid_namespace", return_value=SIBLING_NS),
            patch.object(manager, "reader_pid_namespace", return_value=SIBLING_NS),
            patch("teatree.utils.singleton.pid_alive", return_value=True),
        ):
            won, owner = LoopLease.objects.claim_ownership(
                T3_MASTER_SLOT, session_id="worker-gen2", owner_pid=WORKER_PID
            )

        assert (won, owner) == (False, "worker-gen1")

    def test_the_worker_itself_refreshes_across_a_recreate(self) -> None:
        # The ordinary path: the durable LOOP_RUNNER principal survives the recreate, so
        # the new generation refreshes its own slot immediately rather than waiting a TTL.
        _claim_as_worker()

        with patch.object(manager, "reader_pid_namespace", return_value=SIBLING_NS):
            won, owner = LoopLease.objects.claim_ownership(
                T3_MASTER_SLOT, session_id=LOOP_RUNNER_SESSION_ID, owner_pid=WORKER_PID
            )

        assert (won, owner) == (True, LOOP_RUNNER_SESSION_ID)
        assert LoopLease.objects.get(name=T3_MASTER_SLOT).owner_pid_namespace == SIBLING_NS
