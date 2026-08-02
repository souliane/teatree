"""The worker owns t3-master while it drives loop ticks (#3968).

Before this, nothing ever claimed the slot: the worker drove every registry loop
without holding it, so ``t3 loop owner`` reported "unclaimed" while the two
owner-gated reactive loops skipped forever. The claim and the driving are now one
startup, so the two facts cannot diverge.

The end-to-end arc (claim present ⇒ the loops run; claim absent ⇒ they skip) is the
anti-vacuity proof: :class:`TestGateFollowsTheClaim` goes RED the moment the claim
is removed.
"""

import os
from typing import Any

import pytest

from teatree.core.gates.t3_master_gate import T3MasterGate, t3_master_verdict
from teatree.core.loop_lease_manager import T3_MASTER_SLOT
from teatree.core.models import LoopDriver, LoopLease
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID
from teatree.loops import worker as worker_mod
from teatree.loops.timer_chains import LoopRunnerState
from teatree.loops.worker import LoopWorker, WorkerSeams


class _FakeExecutor:
    def __init__(self, queue: str, worker_id: str) -> None:
        self.queue = queue
        self.worker_id = worker_id
        self.running = True

    def run(self) -> None:
        pass


class _FakeHandle:
    def __init__(self) -> None:
        self.joined = False

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        _ = timeout
        self.joined = True


def _worker(*, polls: int, poll_seconds: float = 0.0, **overrides: Any) -> LoopWorker:
    """A worker that supervises for *polls* polls, then a clean kill-switch OFF."""
    states = iter([LoopRunnerState.ON] * polls)
    noop: dict[str, Any] = {
        "reconcile": lambda: None,
        "seed_chains": lambda: None,
        "expire": lambda: None,
        "kill_ticks": lambda: None,
        "reclaim_leases": lambda: None,
        "claim_master": lambda: None,
        "release_master": lambda: None,
    }
    seams = WorkerSeams(
        read_state=lambda: next(states, LoopRunnerState.OFF),
        make_executor=_FakeExecutor,
        spawn=lambda _executor: _FakeHandle(),
        sleep=lambda _s: None,
        poll_seconds=poll_seconds,
        executor_queues=("loops",),
        **(noop | overrides),
    )
    return LoopWorker(seams)


class TestClaimSeamWiring:
    def test_claims_before_it_begins_driving_ticks(self) -> None:
        order: list[str] = []
        worker = _worker(
            polls=0,
            reconcile=lambda: order.append("reconcile"),
            seed_chains=lambda: order.append("seed"),
            claim_master=lambda: order.append("claim"),
            release_master=lambda: order.append("release"),
        )
        worker.run()

        assert order[0] == "claim", "the claim must land before the chains that drive ticks (#3968)"
        assert order[-1] == "release"

    def test_refreshes_the_claim_on_the_supervisor_poll(self) -> None:
        claims: list[int] = []
        worker = _worker(polls=3, claim_master=lambda: claims.append(1), release_master=lambda: None)
        worker.run()

        assert len(claims) > 1, "the throttled re-claim IS the heartbeat — one startup claim is not enough"

    def test_throttles_the_refresh_well_inside_the_lease_ttl(self) -> None:
        claims: list[int] = []
        # A 5s poll against the 300s refresh cadence: 3 polls must not re-claim.
        worker = _worker(
            polls=3,
            poll_seconds=5.0,
            claim_master=lambda: claims.append(1),
            release_master=lambda: None,
        )
        worker.run()

        assert len(claims) == 1, "the refresh must not write the lease on every 5s kill-switch poll"

    def test_releases_the_claim_on_shutdown(self) -> None:
        releases: list[int] = []
        worker = _worker(polls=1, claim_master=lambda: None, release_master=lambda: releases.append(1))
        worker.run()

        assert releases == [1]

    def test_a_claim_error_never_crashes_the_supervisor(self) -> None:
        def _boom() -> None:
            msg = "db hiccup"
            raise RuntimeError(msg)

        worker = _worker(polls=2, claim_master=_boom, release_master=_boom)
        worker.run()  # must not raise

    def test_defaults_wire_the_real_lease_seams(self) -> None:
        seams = WorkerSeams()

        assert seams.claim_master is worker_mod._claim_t3_master
        assert seams.release_master is worker_mod._release_t3_master


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestClaimAgainstTheRealLease:
    def test_claim_installs_the_runner_principal_and_driver(self) -> None:
        worker_mod._claim_t3_master()

        status = LoopLease.objects.ownership_status(T3_MASTER_SLOT)
        assert status.is_live is True
        assert status.owner_session == LOOP_RUNNER_SESSION_ID
        assert status.driver == LoopDriver.LOOP_RUNNER

    def test_claim_is_idempotent(self) -> None:
        worker_mod._claim_t3_master()
        worker_mod._claim_t3_master()

        assert LoopLease.objects.ownership_status(T3_MASTER_SLOT).owner_session == LOOP_RUNNER_SESSION_ID

    def test_claim_never_evicts_a_live_foreign_session(self) -> None:
        # A DIFFERENT alive pid — same-pid would be the #2835 same-process self-reclaim,
        # which legitimately wins and would make this assertion test nothing.
        LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id="sess-live", owner_pid=os.getppid())

        worker_mod._claim_t3_master()

        assert LoopLease.objects.ownership_status(T3_MASTER_SLOT).owner_session == "sess-live"

    def test_release_is_a_no_op_for_a_slot_taken_over_by_a_session(self) -> None:
        worker_mod._claim_t3_master()
        LoopLease.objects.take_over_ownership(T3_MASTER_SLOT, session_id="sess-took-over", owner_pid=os.getpid())

        worker_mod._release_t3_master()

        assert LoopLease.objects.ownership_status(T3_MASTER_SLOT).owner_session == "sess-took-over"


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestGateFollowsTheClaim:
    """Anti-vacuity: delete the worker's claim and both assertions below invert."""

    def test_gate_runs_while_the_worker_holds_the_slot(self) -> None:
        worker_mod._claim_t3_master()

        assert t3_master_verdict(caller_session="any-session").outcome is T3MasterGate.RUN

    def test_gate_skips_unclaimed_once_the_worker_releases(self) -> None:
        worker_mod._claim_t3_master()
        worker_mod._release_t3_master()

        assert t3_master_verdict(caller_session="any-session").outcome is T3MasterGate.UNCLAIMED
