"""The long-lived ``t3 worker`` — the singleton executor pool for the timer chains (#1796).

One process runs programmatic ``django_tasks_db`` :class:`Worker` executor threads —
a host-scaled ``loops`` pool (floored at 2, :func:`loops_executor_count`), and ``default``
(coding) and ``cheap`` (review/draining) pools sized every poll to the headless lane
widths, one executor per seat the admission verdict can hand out
(:func:`agent_executor_queues`) — so a heavy headless ``default`` job can never starve a
reactive loop timer, two slow loop ticks can never stall every OTHER loop's timer, and
every admitted seat has a thread to run on.
A supervisor thread re-reads the fleet admission verdict every ~5 s AND
polls each executor thread's :meth:`is_alive`, respawning any that a swallowed error
(a ``DBTaskResult`` ``OperationalError`` inside ``db_worker``) silently killed — so a
dead executor never freezes the whole box while the process still looks healthy. When the
control database is locked, the executor waits and re-enters ``run`` in the same thread;
a ten-minute healthy run refunds a respawn's crash budget. When the
active preset admits ZERO loops it stops every executor and keeps POLLING (C1): the
schedule boundary that will admit work again lives in this process, so exiting would leave
nobody to notice it. A SIGTERM/SIGINT is what ends the process — joining and, after the
join timeout, SIGKILLing any in-flight tick process group the join left orphaned;
when a single executor exhausts its respawn budget the worker checkpoints in-flight
agents before exiting NON-ZERO
(loud, never silent) so the OS/container restarts it fresh rather than limping with a
dead pool. The flock singleton (:func:`teatree.utils.singleton.singleton`) guarantees
at most one worker per box. At startup the worker reconciles the loop-timer chains, seeds
the maintenance chains — including the ``drive_off_live_tick_loops`` chain that fires
the tick command of every ``off_live_tick`` loop, the ONLY driver those loops have — and
expires stale ``default``- and ``cheap``-queue jobs BEFORE spawning executors (so a box that
queued days-old provision/ship jobs while no worker ran never blind-fires them on the
default-ON flip), so a fresh or crash-recovered box catches up and
self-heals with no OS scheduler (no cron / launchd / systemd). The worker supervisor +
reconciler IS the process-watchdog surface.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from teatree.agents.runner_heartbeat import CHECKPOINT_GRACE_SECONDS
from teatree.core.admission.lanes import ANSWERING_LANE_WIDTH
from teatree.core.admission_pressure import MACHINE_BRAKE_CAUSES
from teatree.core.managers_task_claim import process_exit
from teatree.core.modelkit.phases import MAINTENANCE_PHASES, CheapClass
from teatree.loop.queue_drain import expire_stale_headless_jobs
from teatree.loops.deadlined_tick import kill_live_tick_process_groups
from teatree.loops.enable_verdict import FleetAdmission, read_fleet_admission
from teatree.loops.executor_thread import (
    EXECUTOR_HEALTH_WINDOW_SECONDS,
    EXECUTOR_INTERVAL_SECONDS,
    CheckpointControl,
    checkpoint_executor_threads,
)
from teatree.loops.executor_thread import Executor as _Executor
from teatree.loops.executor_thread import Handle as _Handle
from teatree.loops.executor_thread import build_executor as _build_executor
from teatree.loops.executor_thread import spawn_executor_thread as _spawn_executor_thread
from teatree.loops.timer_reconciler import ensure_loop_timers, ensure_maintenance_chains
from teatree.loops.worker_health import publish_worker_heartbeat
from teatree.loops.worker_lease_cleanup import _reap_expired_leases, _reclaim_dead_owner_leases
from teatree.utils.host_pressure_sampler import HostSampler
from teatree.utils.ram_probe import default_provision_concurrency

if TYPE_CHECKING:
    from teatree.core.admission_governor import AdmissionDecision
    from teatree.core.agent_admission import LaneWidths

logger = logging.getLogger(__name__)

#: The prior hardcoded ``loops``-queue width; now the FLOOR so a small box keeps the
#: old minimum (2 reactive-timer threads) while a bigger box scales up — two slow loop
#: ticks pinning both floor threads no longer stalls every OTHER loop's timer.
LOOPS_EXECUTOR_FLOOR = 2
#: The ``default`` pool's floor when the lane widths are unreadable and it falls back to host size.
DEFAULT_QUEUE_FLOOR = 2


def loops_executor_count() -> int:
    """Host-scaled width of the ``loops`` (reactive-timer) executor pool, floored at 2.

    A fixed 2 threads serialise every loop timer fire behind at most two in-flight
    ticks, so two slow ticks stall every other loop's timer plus the maintenance /
    reconcile chains. Scaling with the shared PR-01 resource ceiling
    (:func:`default_provision_concurrency` — half the logical cores) lets a bigger
    box fire more timers in parallel; the floor preserves the prior minimum on a
    1-2 core box.
    """
    return max(LOOPS_EXECUTOR_FLOOR, default_provision_concurrency())


def agent_executor_queues(widths: "LaneWidths | None") -> tuple[str, ...]:
    """One executor per seat the verdict can admit, so no admitted seat lapses waiting for a thread."""
    if widths is None:
        # The fallback keeps one executor for every reserved class.
        return ("default",) * max(DEFAULT_QUEUE_FLOOR, default_provision_concurrency()) + ("cheap",) * (
            len(CheapClass) + ANSWERING_LANE_WIDTH + 1 + len(MAINTENANCE_PHASES)
        )
    return ("default",) * widths.expensive + ("cheap",) * (
        widths.cheap + widths.answering + widths.architectural_review + widths.maintenance
    )


def _read_pool_pressure() -> "AdmissionDecision | None":
    """Live governor verdict for the pool; its own probe failure is fail-open."""
    from teatree.loop.admission import governor_verdict  # noqa: PLC0415 — loop orchestration at call time
    from teatree.loop.statusline import default_path  # noqa: PLC0415 — same sidecar as dispatch

    return governor_verdict(statusline_path=default_path())


def _read_agent_queues() -> tuple[str, ...]:
    """The agent pool for the live lane widths; an unreadable width keeps the host-sized pool."""
    from teatree.core.agent_admission import headless_lane_widths  # noqa: PLC0415 — ORM import needs the app registry

    try:
        widths = headless_lane_widths()
    except Exception:
        logger.exception("headless lane widths unreadable — sizing the agent pool from the host")
        widths = None
    return agent_executor_queues(widths)


#: The supervisor re-reads the fleet verdict on this cadence — a preset that stops
#: admitting work stops further dispatch within ~this many seconds.
SUPERVISOR_POLL_SECONDS = 5.0
#: How often the supervisor re-claims ``t3-master`` (#3968). Well inside the 1800 s
#: lease TTL, and far rarer than the 5 s kill-switch poll so the heartbeat adds no
#: meaningful write pressure to the control DB. The re-claim also self-heals the slot
#: once a ``t3 loop claim --take-over`` by a since-dead session goes stale — which is
#: the "a transient session's claim rots" shape the ticket warns against.
T3_MASTER_REFRESH_SECONDS = 300.0
#: A single executor slot's respawn budget; a ten-minute healthy run resets it.
#: Exhaustion makes the worker exit NON-ZERO (a crash-looping executor
#: is a real fault the OS/container should restart the whole worker for, not one the
#: supervisor should mask by respawning forever).
MAX_EXECUTOR_RESPAWNS = 5
#: How many consecutive supervisor polls the fleet verdict may read UNREADABLE before the
#: worker exits NON-ZERO so the container's restart policy restarts it (F7). A transient blip
#: recovers within a poll or two; a persistent read failure is a real fault, never a
#: clean stop that leaves the factory silently dead.
MAX_UNREADABLE_POLLS = 3


class _HealthPublisher(Protocol):
    def __call__(self, admission: str, *, active: bool) -> None: ...


class LoopWorkerExecutorCrashError(RuntimeError):
    """A ``loops``/``default`` executor thread died and exhausted its respawn budget.

    Raised out of :meth:`LoopWorker.run` (after the pool is torn down) so the worker
    process exits NON-ZERO: a repeatedly-crashing executor is a genuine fault the
    OS/container must restart the worker for, never one the supervisor silently masks.
    """


class LoopWorkerExecutorStopError(RuntimeError):
    """Executors survived the shutdown join AND the tick kill — the pool never stopped."""


class FleetAdmissionUnreadableError(RuntimeError):
    """The fleet verdict read UNREADABLE for too many consecutive polls (F7).

    Raised out of :meth:`LoopWorker.run` so the worker exits NON-ZERO: a persistent
    enable-plane read failure is a real fault the supervisor must restart the worker
    for, never a clean exit-0, which ``restart: unless-stopped`` spins into a silent
    boot loop. A preset admitting nothing is a deliberate stop the process stays alive
    through; only "cannot confirm" crashes.
    """


_CRASH_MESSAGE = "A loops/default executor thread died and exhausted its respawn budget; exiting non-zero."
_STOP_MESSAGE = "The executor pool did not stop within its bounded grace period; exiting non-zero."
_UNREADABLE_MESSAGE = "The fleet admission verdict was unreadable for too many polls; exiting non-zero."


def _claim_t3_master() -> None:
    """Claim/refresh the machine-wide ``t3-master`` slot for this worker (#3968).

    Nothing claimed it before, so ``t3 loop owner`` reported "unclaimed" while this
    process drove every registry loop — and the two owner-gated reactive loops
    (``loop_slack_answer`` / ``loop_self_improve``) deferred forever to an owner that
    never existed. The principal is the durable :data:`LOOP_RUNNER_SESSION_ID`
    constant rather than a per-process id so a worker restart is never locked out of
    its own lease for a full TTL (#3810); liveness rides on ``owner_pid`` instead.

    The claim is a CAS that never evicts a live owner: an interactive session holding
    the slot keeps it, and this worker takes it over on a later refresh once that
    lease lapses.
    """
    from teatree.core.loop_lease_manager import T3_MASTER_SLOT  # noqa: PLC0415 — deferred: pulls in django.db
    from teatree.core.models import LoopDriver, LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID  # noqa: PLC0415 — deferred: cheap, kept local

    won, owner = LoopLease.objects.claim_ownership(
        T3_MASTER_SLOT,
        session_id=LOOP_RUNNER_SESSION_ID,
        owner_pid=os.getpid(),
        driver=LoopDriver.LOOP_RUNNER,
    )
    if not won:
        logger.warning(
            "t3-master is held by live session %r; this worker drives loop ticks without owning the slot, so the "
            "owner-gated reactive loops defer to that session until its lease lapses (#3968).",
            owner,
        )


def _release_t3_master() -> None:
    """Hand ``t3-master`` back at shutdown — CAS'd, so a session take-over is untouched."""
    from teatree.core.loop_lease_manager import T3_MASTER_SLOT  # noqa: PLC0415 — deferred: pulls in django.db
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID  # noqa: PLC0415 — deferred: cheap, kept local

    LoopLease.objects.release_ownership(T3_MASTER_SLOT, session_id=LOOP_RUNNER_SESSION_ID)


def _start_live_ingress() -> None:
    """Publish this worker's live socket at boot, so a worker running nothing answers ``live list`` with none."""
    from teatree.agents.live_orchestrator import start_shared_orchestrator_peer  # noqa: PLC0415 — defer agents runtime

    try:
        start_shared_orchestrator_peer()
    except Exception:
        logger.warning("Live ingress could not start; operators cannot reach this worker's sessions", exc_info=True)


def _publish_health(admission: str, *, active: bool) -> None:
    try:
        publish_worker_heartbeat(admission, active=active)
    except OSError:
        logger.warning(
            "Worker health heartbeat could not be written; private availability will fail closed.", exc_info=True
        )


@dataclass(frozen=True)
class WorkerSeams:
    """The injectable collaborators — the production defaults wire the real seams.

    Grouped so the supervision/lifecycle logic is tested without real threads, a
    real DB, or a real clock, while keeping :class:`LoopWorker`'s constructor thin.
    """

    read_admission: Callable[[], FleetAdmission] = read_fleet_admission
    read_pressure: Callable[[], "AdmissionDecision | None"] = _read_pool_pressure
    reconcile: Callable[[], object] = ensure_loop_timers
    seed_chains: Callable[[], object] = ensure_maintenance_chains
    expire: Callable[[], object] = expire_stale_headless_jobs
    make_executor: Callable[[str, str], _Executor] = _build_executor
    spawn: Callable[[_Executor], _Handle] = _spawn_executor_thread
    kill_ticks: Callable[[], object] = kill_live_tick_process_groups
    reclaim_leases: Callable[[], object] = _reclaim_dead_owner_leases
    reap_leases: Callable[[], object] = _reap_expired_leases
    claim_master: Callable[[], object] = _claim_t3_master
    release_master: Callable[[], object] = _release_t3_master
    publish_host_pressure: Callable[[], object] = field(default_factory=lambda: HostSampler().publish_if_due)
    start_live_ingress: Callable[[], object] = _start_live_ingress
    publish_health: _HealthPublisher = _publish_health
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    begin_exit: Callable[[str], None] = process_exit.begin
    poll_seconds: float = SUPERVISOR_POLL_SECONDS
    master_refresh_seconds: float = T3_MASTER_REFRESH_SECONDS
    max_respawns: int = MAX_EXECUTOR_RESPAWNS
    max_unreadable_polls: int = MAX_UNREADABLE_POLLS
    read_agent_queues: Callable[[], tuple[str, ...]] = _read_agent_queues
    loops_executors: int = field(default_factory=loops_executor_count)


@dataclass
class _Slot:
    """One executor thread plus the queue + respawn bookkeeping to resurrect it."""

    queue: str
    index: int
    executor: _Executor
    handle: _Handle
    respawns: int = 0
    spawned_at: float = 0.0


class LoopWorker:
    """Supervised executor pool: reconcile, drain K queues, quiesce while the preset admits nothing, stop on signal."""

    def __init__(self, seams: WorkerSeams | None = None) -> None:
        self._seams = seams or WorkerSeams()
        self._stop = threading.Event()
        self._slots: list[_Slot] = []
        self._retiring: list[_Slot] = []
        self._next_slot_id = 0
        self._polls_since_master_refresh = 0
        self._brake_reason: str | None = None

    def request_stop(self) -> None:
        """Signal the supervisor to shut down (the SIGTERM/SIGINT handler target)."""
        self._stop.set()

    def _spawn_slot(self, queue: str, index: int, *, respawns: int = 0) -> _Slot:
        executor = self._seams.make_executor(queue, f"worker-{os.getpid()}-{index}-{queue}")
        return _Slot(
            queue=queue,
            index=index,
            executor=executor,
            handle=self._seams.spawn(executor),
            respawns=respawns,
            spawned_at=self._seams.monotonic(),
        )

    def _resize_pool(self, desired: tuple[str, ...]) -> None:
        """Quiesce surplus slots, then refill only when retired threads have exited."""
        remaining = list(self._slots)
        kept: list[_Slot] = []
        wanted: list[str] = []
        for queue in desired:
            match = next((slot for slot in remaining if slot.queue == queue), None)
            if match is None:
                wanted.append(queue)
            else:
                remaining.remove(match)
                kept.append(match)
        for slot in remaining:
            slot.executor.running = False
            slot.handle.join(timeout=0)
            self._retiring.append(slot)
        self._slots = kept
        self._retiring = [slot for slot in self._retiring if slot.handle.is_alive()]
        for queue in wanted:
            if len(self._slots) + len(self._retiring) >= len(desired):
                break
            index = self._next_slot_id
            self._next_slot_id += 1
            self._slots.append(self._spawn_slot(queue, index))

    def _stop_pool(self) -> list[_Slot]:
        """Drain active and retiring executors; report survivors after bounded joins."""
        slots = self._slots + self._retiring
        self._slots, self._retiring = [], []
        for slot in slots:
            slot.executor.running = False
        for slot in slots:
            slot.handle.join(timeout=EXECUTOR_INTERVAL_SECONDS * 3)
        return [slot for slot in slots if slot.handle.is_alive()]

    def _log_brake_transition(self, pressure: "AdmissionDecision | None") -> None:
        """Log a brake as it engages and releases, never on every 5 s poll it holds."""
        brake_reason = pressure.reason if pressure is not None and not pressure.admit else None
        if brake_reason == self._brake_reason:
            return
        if brake_reason is not None:
            logger.warning(
                "admission governor braked coding refill; retaining control and cheap lanes: %s", brake_reason
            )
        else:
            logger.info("admission governor released the brake; refilling the coding lanes")
        self._brake_reason = brake_reason

    def _match_pool_to(self, admission: FleetAdmission) -> None:
        """Hold the pool where the fleet verdict says it should be — spawn or quiesce.

        Quiescing keeps the PROCESS alive: the schedule boundary that re-admits work is
        driven from this supervisor, so an exit here would leave nobody to observe it and
        the factory would stay stopped past the posture that stopped it.
        """
        if admission is FleetAdmission.ADMITS:
            pressure = self._seams.read_pressure()
            self._log_brake_transition(pressure)
            loops = ("loops",) * self._seams.loops_executors
            agents = self._seams.read_agent_queues()
            if pressure is not None and not pressure.admit:
                # Timer/reconciliation tasks are the control plane: retiring every
                # executor would also disable the monitor that diagnoses the brake.
                # Cheap agents are exempt from MACHINE pressure only, never a
                # spent token budget or collapsed yield. Unknown causes fail
                # closed for agent execution while control diagnosis continues.
                exempt = pressure.cause in MACHINE_BRAKE_CAUSES
                agents = tuple(queue for queue in agents if exempt and queue == "cheap")
            elif not self._slots:
                logger.info("the active preset admits work again — restarting the executor pool")
            self._resize_pool(loops + agents)
            return
        if self._slots:
            logger.warning(
                "the active preset admits ZERO loops — stopping the executor pool; this process stays alive so the "
                "next schedule boundary still re-admits work. `t3 loop preset show` names the posture."
            )
            self._resize_pool(())

    def _respawn_dead_executors(self) -> _Slot | None:
        """Respawn dead executor threads and return the slot that exhausted its budget.

        A ``db_worker`` executor thread that hits a swallowed error (a ``DBTaskResult``
        ``OperationalError``) exits silently — the pinned queue then never drains and
        every timer chain on it freezes machine-wide while the process still looks
        healthy. Polling :meth:`is_alive` and respawning keeps the pool live; a slot
        that keeps dying past :attr:`WorkerSeams.max_respawns` is a real fault, so the
        caller exits the worker NON-ZERO instead of masking it.
        """
        for i, slot in enumerate(self._slots):
            if slot.handle.is_alive():
                if slot.respawns and self._seams.monotonic() - slot.spawned_at >= EXECUTOR_HEALTH_WINDOW_SECONDS:
                    slot.respawns = 0
                continue
            if slot.respawns >= self._seams.max_respawns:
                logger.error(
                    "Executor for queue %r (slot %d) died %d times — giving up; the worker will exit non-zero.",
                    slot.queue,
                    slot.index,
                    slot.respawns + 1,
                )
                return slot
            logger.warning(
                "Executor for queue %r (slot %d) died; respawning (respawn %d).",
                slot.queue,
                slot.index,
                slot.respawns + 1,
            )
            self._slots[i] = self._spawn_slot(slot.queue, slot.index, respawns=slot.respawns + 1)
        return None

    def _reclaim_dead_owner_leases(self) -> None:
        """Sweep dead-owner loop leases; a reclaim error must never crash the supervisor (#3571)."""
        try:
            self._seams.reclaim_leases()
        except Exception:
            logger.warning("Dead-owner loop-lease reclaim failed this poll; will retry next tick.", exc_info=True)

    def _reap_expired_leases(self) -> None:
        """Reap expired work-lease debris; a reap error must never crash the supervisor (#4253)."""
        try:
            self._seams.reap_leases()
        except Exception:
            logger.warning("Expired loop-lease reap failed this beat; will retry next beat.", exc_info=True)

    def _claim_t3_master(self) -> None:
        """Claim/refresh t3-master; a claim error must never crash the supervisor (#3968)."""
        try:
            self._seams.claim_master()
        except Exception:
            logger.warning("t3-master claim failed; will retry on the next refresh.", exc_info=True)

    def _release_t3_master(self) -> None:
        """Hand t3-master back; a release error must never mask the shutdown reason (#3968)."""
        try:
            self._seams.release_master()
        except Exception:
            logger.warning("t3-master release failed; the lease will lapse on its TTL.", exc_info=True)

    def _polls_per_master_refresh(self) -> int:
        """Supervisor polls between two t3-master re-claims, floored at one.

        A zero/negative poll interval (the test seam, and a degenerate config) would
        divide by zero, so it degrades to re-claiming every poll — the conservative
        end, never a skipped heartbeat.
        """
        seams = self._seams
        if seams.poll_seconds <= 0:
            return 1
        return max(1, round(seams.master_refresh_seconds / seams.poll_seconds))

    def _per_poll_maintenance(self) -> None:
        """The supervisor's per-poll upkeep: the throttled lease beat, then the dead-owner sweep.

        The throttled beat carries both lease writes — the t3-master heartbeat and the
        expired-debris reap — because both are cadence work on one table that would add
        needless control-DB write pressure at the 5 s kill-switch poll.
        """
        try:
            self._seams.publish_host_pressure()
        except Exception:
            logger.warning("host pressure publish failed; will retry on the next poll.", exc_info=True)
        self._polls_since_master_refresh += 1
        if self._polls_since_master_refresh >= self._polls_per_master_refresh():
            self._polls_since_master_refresh = 0
            self._claim_t3_master()
            self._reap_expired_leases()
        self._reclaim_dead_owner_leases()

    def _admission_for_poll(self, unreadable_polls: int) -> tuple[FleetAdmission, int, bool]:
        admission = self._seams.read_admission()
        if admission is FleetAdmission.UNREADABLE:
            unreadable_polls += 1
            logger.warning(
                "fleet verdict unreadable (%d/%d consecutive polls) — will crash-restart if it persists",
                unreadable_polls,
                self._seams.max_unreadable_polls,
            )
            self._seams.publish_health(admission.value, active=False)
            return admission, unreadable_polls, unreadable_polls >= self._seams.max_unreadable_polls
        self._match_pool_to(admission)
        return admission, 0, False

    @staticmethod
    def _raise_on_exit(*, stranded: bool, crashed: bool, unreadable: bool) -> None:
        if stranded:
            raise LoopWorkerExecutorStopError(_STOP_MESSAGE)
        if crashed:
            raise LoopWorkerExecutorCrashError(_CRASH_MESSAGE)
        if unreadable:
            raise FleetAdmissionUnreadableError(_UNREADABLE_MESSAGE)

    def run(self) -> None:
        """Reconcile, expire stale jobs, then supervise the pool against the fleet verdict until stopped."""
        seams = self._seams
        # Ownership and driving are ONE startup (#3968): the slot is claimed before the
        # chains that fire ticks exist, so `t3 loop owner` can never report "unclaimed"
        # while this process drives loops.
        self._claim_t3_master()
        seams.start_live_ingress()
        seams.reconcile()
        seams.seed_chains()
        # Expire the stale `default`-queue backlog BEFORE any executor spawns, so a box
        # that queued days-old provision/ship jobs while no worker ran never blind-fires
        # them the instant the worker starts (the default-ON flip's load-jam class).
        seams.expire()

        crashed = False
        unreadable = False
        exit_reason = ""
        unreadable_polls = 0
        try:
            while not self._stop.is_set():
                admission, unreadable_polls, unreadable = self._admission_for_poll(unreadable_polls)
                if unreadable:
                    exit_reason = "this worker is exiting non-zero: fleet admission unreadable"
                    break
                seams.sleep(seams.poll_seconds)
                exhausted = self._respawn_dead_executors() if self._slots else None
                if exhausted is not None:
                    crashed = True
                    exit_reason = (
                        f"this worker is exiting non-zero: executor for queue {exhausted.queue} "
                        f"(slot {exhausted.index}) died {exhausted.respawns + 1} times"
                    )
                    break
                self._per_poll_maintenance()
                if not self._stop.is_set() and admission is not FleetAdmission.UNREADABLE:
                    degraded = any(slot.handle.degraded for slot in self._slots)
                    seams.publish_health(
                        "degraded" if degraded else admission.value, active=bool(self._slots) and not degraded
                    )
        except BaseException as failure:
            exit_reason = f"this worker is exiting non-zero: supervisor failed with {type(failure).__name__}"
            raise
        finally:
            if exit_reason:
                checkpoint_executor_threads(
                    self._slots + self._retiring,
                    exit_reason,
                    CHECKPOINT_GRACE_SECONDS,
                    seams,
                    CheckpointControl(self._stop.is_set, self._claim_t3_master, seams.master_refresh_seconds),
                )
            self.request_stop()
            survivors = self._stop_pool()
            # The daemon-join above never reaches a tick SUBPROCESS: a SIGTERM mid-tick
            # orphans it with no deadline owner. Kill any in-flight tick process group so
            # no zombie/orphan outlives the worker's shutdown.
            seams.kill_ticks()
            for slot in survivors:
                slot.handle.join(timeout=EXECUTOR_INTERVAL_SECONDS * 3)
            stranded = any(slot.handle.is_alive() for slot in survivors)
            # Hand t3-master back so a restarting worker (or an operator's session)
            # finds an unowned slot instead of waiting out this process's TTL.
            self._release_t3_master()
            if exit_reason and not stranded:
                seams.begin_exit("")
        self._raise_on_exit(stranded=stranded, crashed=crashed, unreadable=unreadable)
