"""The long-lived ``t3 worker`` — the singleton executor pool for the timer chains (#1796).

One process runs programmatic ``django_tasks_db`` :class:`Worker` executor threads —
a host-scaled ``loops`` pool (floored at 2, :func:`loops_executor_count`) and a
host-scaled ``default`` pool (floored at 2, :func:`default_queue_executor_count`) and
one protected ``cheap`` executor for review/draining phases —
so a heavy headless ``default`` job can never starve a reactive loop timer, two slow
loop ticks can never stall every OTHER loop's timer, and a deep backlog of independent
headless work still drains in parallel on a bigger box instead of one-or-two-at-a-time.
A supervisor thread re-reads the fleet admission verdict every ~5 s AND
polls each executor thread's :meth:`is_alive`, respawning any that a swallowed error
(a ``DBTaskResult`` ``OperationalError`` inside ``db_worker``) silently killed — so a
dead executor never freezes the whole box while the process still looks healthy. When the
active preset admits ZERO loops it stops every executor and keeps POLLING (C1): the
schedule boundary that will admit work again lives in this process, so exiting would leave
nobody to notice it. A SIGTERM/SIGINT is what ends the process — joining and, after the
join timeout, SIGKILLing any in-flight tick process group the join left orphaned;
when a single executor exhausts its respawn budget the worker exits NON-ZERO
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
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from teatree.core.admission_pressure import MACHINE_BRAKE_CAUSES
from teatree.loop.queue_drain import expire_stale_headless_jobs
from teatree.loops.deadlined_tick import kill_live_tick_process_groups
from teatree.loops.enable_verdict import FleetAdmission, read_fleet_admission
from teatree.loops.timer_reconciler import ensure_loop_timers, ensure_maintenance_chains
from teatree.loops.worker_health import publish_worker_heartbeat
from teatree.utils.ram_probe import default_provision_concurrency
from teatree.utils.thread_db import close_thread_db_connections

if TYPE_CHECKING:
    from django_tasks_db.management.commands.db_worker import Worker

    from teatree.core.admission_governor import AdmissionDecision

logger = logging.getLogger(__name__)

#: The prior hardcoded ``loops``-queue width; now the FLOOR so a small box keeps the
#: old minimum (2 reactive-timer threads) while a bigger box scales up — two slow loop
#: ticks pinning both floor threads no longer stalls every OTHER loop's timer.
LOOPS_EXECUTOR_FLOOR = 2
#: The prior hardcoded ``default``-queue width; now the FLOOR so a small box keeps
#: the old minimum while a bigger box scales up.
DEFAULT_QUEUE_FLOOR = 2
MIN_PARTITIONED_CEILING = 2


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


def default_queue_executor_count() -> int:
    """Host-scaled width of the ``default`` coding executor pool, floored at 2.

    A deep backlog of independent coding jobs drained through a fixed 2 threads
    one-or-two-at-a-time regardless of host size. Scaling with the shared
    PR-01 resource ceiling (:func:`default_provision_concurrency` — half the logical
    cores) lets an idle multi-core box run more phase work in parallel; the floor
    preserves the prior minimum on a 1-2 core box.
    """
    return max(DEFAULT_QUEUE_FLOOR, default_provision_concurrency())


def build_executor_queues() -> tuple[str, ...]:
    """Control and coding pools plus one protected cheap/draining executor."""
    return ("loops",) * loops_executor_count() + ("default",) * default_queue_executor_count() + ("cheap",)


def _bounded_executor_queues(queues: tuple[str, ...], ceiling: int) -> tuple[str, ...]:
    """Clamp width, protecting control/review before filling coding capacity."""
    if ceiling >= len(queues):
        return queues
    names = tuple(dict.fromkeys(queues))
    if ceiling == MIN_PARTITIONED_CEILING and names == ("loops", "default", "cheap"):
        # The ceiling is for live agents, not waiting worker threads. A third
        # thread keeps a review executable while one coding agent is active.
        return names
    if ceiling == 1 and len(names) > 1:
        # One executor can subscribe to both queues; assigning it to either
        # queue alone would permanently starve the other at a one-slot ceiling.
        return (",".join(names),)
    remaining = Counter(queues)
    if names == ("loops", "default", "cheap") and ceiling >= len(names):
        # A round-robin clamp spends the fourth slot on a second control
        # executor while coding has only one. Protect one control and one
        # review lane, then give available capacity to coding first.
        coding = min(remaining["default"], ceiling - 2)
        selected = ["loops", *(["default"] * coding), "cheap"]
        remaining["loops"] -= 1
        remaining["default"] -= coding
        remaining["cheap"] -= 1
        for name in ("default", "loops", "cheap"):
            extra = min(ceiling - len(selected), remaining[name])
            selected.extend([name] * extra)
        return tuple(selected)
    selected: list[str] = []
    while len(selected) < max(0, ceiling):
        for name in names:
            if remaining[name] and len(selected) < ceiling:
                selected.append(name)
                remaining[name] -= 1
    return tuple(selected)


def _read_pool_pressure() -> "AdmissionDecision | None":
    """Live governor verdict for the pool; its own probe failure is fail-open."""
    from teatree.loop.admission import governor_verdict  # noqa: PLC0415 — loop orchestration at call time
    from teatree.loop.statusline import default_path  # noqa: PLC0415 — same sidecar as dispatch

    return governor_verdict(statusline_path=default_path())


#: The supervisor re-reads the fleet verdict on this cadence — a preset that stops
#: admitting work stops further dispatch within ~this many seconds.
SUPERVISOR_POLL_SECONDS = 5.0
#: How often the supervisor re-claims ``t3-master`` (#3968). Well inside the 1800 s
#: lease TTL, and far rarer than the 5 s kill-switch poll so the heartbeat adds no
#: meaningful write pressure to the control DB. The re-claim also self-heals the slot
#: once a ``t3 loop claim --take-over`` by a since-dead session goes stale — which is
#: the "a transient session's claim rots" shape the ticket warns against.
T3_MASTER_REFRESH_SECONDS = 300.0
#: Each executor's empty-poll interval — small so a requested stop flips fast.
EXECUTOR_INTERVAL_SECONDS = 1.0
#: How many times a single executor slot may be respawned within one worker
#: lifetime before the worker gives up and exits NON-ZERO (a crash-looping executor
#: is a real fault the OS/container should restart the whole worker for, not one the
#: supervisor should mask by respawning forever).
MAX_EXECUTOR_RESPAWNS = 5
#: How many consecutive supervisor polls the fleet verdict may read UNREADABLE before the
#: worker exits NON-ZERO so the container's restart policy restarts it (F7). A transient blip
#: recovers within a poll or two; a persistent read failure is a real fault, never a
#: clean stop that leaves the factory silently dead.
MAX_UNREADABLE_POLLS = 3


class _Executor(Protocol):
    running: bool

    def run(self) -> None: ...


class _Handle(Protocol):
    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...


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


def _build_executor(queue_name: str, worker_id: str) -> "Worker":
    """A programmatic ``db_worker`` executor drained forever on ONE queue."""
    from django.tasks import DEFAULT_TASK_BACKEND_ALIAS  # noqa: PLC0415 — deferred: Django import at call time
    from django_tasks_db.management.commands.db_worker import Worker  # noqa: PLC0415 — deferred: heavy/optional dep

    return Worker(
        queue_names=queue_name.split(","),
        interval=EXECUTOR_INTERVAL_SECONDS,
        batch=False,
        backend_name=DEFAULT_TASK_BACKEND_ALIAS,
        startup_delay=False,
        max_tasks=None,
        worker_id=worker_id,
        excluded_queue_names=[],
    )


def _reclaim_dead_owner_leases() -> None:
    """Return every ``loop:<name>``/``t3-master`` lease held by a dead session to the pool (#3571).

    The worker supervisor's runtime half of the dead-owner reclaim (``run_boot_sweeps``
    owns the boot half): a loop whose owning session crashed — or whose pid was reused /
    lives in another container namespace — is otherwise SKIPped by the live worker
    forever. Lazy-imported so the module's import graph carries no Django/ORM edge.
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

    LoopLease.objects.reclaim_dead_owner_leases()


def _reap_expired_leases() -> None:
    """Delete long-expired work-lease rows nothing will consult again (#4253).

    ``acquire`` mints a ``work:<kind>:<hash>`` row per unit of work and nothing retires
    it, so the table grows without bound and an operator reading it cannot tell a live
    holder from hours of debris. Owner slots are never in range — their ``generation`` is
    the fencing token — so this is disjoint from the reclaim above.
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

    LoopLease.objects.reap_expired_leases()


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


def _spawn_executor_thread(executor: _Executor) -> _Handle:
    """Run *executor* in a daemon thread that closes its DB connection on exit.

    Closes the raw DB-API handle rather than calling ``connections.close_all()``:
    that is a documented no-op under the in-memory test database, so it left this
    thread's handle stranded for a later GC. See :mod:`teatree.utils.thread_db`.
    """

    def _run() -> None:
        try:
            executor.run()
        finally:
            close_thread_db_connections()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


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
    publish_health: _HealthPublisher = _publish_health
    sleep: Callable[[float], None] = time.sleep
    poll_seconds: float = SUPERVISOR_POLL_SECONDS
    master_refresh_seconds: float = T3_MASTER_REFRESH_SECONDS
    max_respawns: int = MAX_EXECUTOR_RESPAWNS
    max_unreadable_polls: int = MAX_UNREADABLE_POLLS
    executor_queues: tuple[str, ...] = field(default_factory=build_executor_queues)


@dataclass
class _Slot:
    """One executor thread plus the queue + respawn bookkeeping to resurrect it."""

    queue: str
    index: int
    executor: _Executor
    handle: _Handle
    respawns: int = 0


class LoopWorker:
    """Supervised executor pool: reconcile, drain K queues, quiesce while the preset admits nothing, stop on signal."""

    def __init__(self, seams: WorkerSeams | None = None) -> None:
        self._seams = seams or WorkerSeams()
        self._stop = threading.Event()
        self._slots: list[_Slot] = []
        self._retiring: list[_Slot] = []
        self._next_slot_id = 0
        self._polls_since_master_refresh = 0

    def request_stop(self) -> None:
        """Signal the supervisor to shut down (the SIGTERM/SIGINT handler target)."""
        self._stop.set()

    def _spawn_slot(self, queue: str, index: int, *, respawns: int = 0) -> _Slot:
        executor = self._seams.make_executor(queue, f"worker-{os.getpid()}-{index}-{queue}")
        return _Slot(queue=queue, index=index, executor=executor, handle=self._seams.spawn(executor), respawns=respawns)

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

    def _match_pool_to(self, admission: FleetAdmission) -> None:
        """Hold the pool where the fleet verdict says it should be — spawn or quiesce.

        Quiescing keeps the PROCESS alive: the schedule boundary that re-admits work is
        driven from this supervisor, so an exit here would leave nobody to observe it and
        the factory would stay stopped past the posture that stopped it.
        """
        if admission is FleetAdmission.ADMITS:
            pressure = self._seams.read_pressure()
            if pressure is not None and not pressure.admit:
                if self._slots:
                    logger.warning(
                        "admission governor braked coding refill; retaining control and cheap lanes: %s",
                        pressure.reason,
                    )
                # Timer/reconciliation tasks are the control plane: retiring every
                # executor would also disable the monitor that diagnoses the brake.
                # Cheap agents are exempt from MACHINE pressure only, never a
                # spent token budget or collapsed yield. Unknown causes fail
                # closed for agent execution while control diagnosis continues.
                desired = ("loops", "cheap") if pressure.cause in MACHINE_BRAKE_CAUSES else ("loops",)
                self._resize_pool(desired)
                return
            desired = self._seams.executor_queues
            if pressure is not None:
                desired = _bounded_executor_queues(desired, pressure.ceiling)
            if not self._slots:
                logger.info("the active preset admits work again — restarting the executor pool")
            self._resize_pool(desired)
            return
        if self._slots:
            logger.warning(
                "the active preset admits ZERO loops — stopping the executor pool; this process stays alive so the "
                "next schedule boundary still re-admits work. `t3 loop preset show` names the posture."
            )
            self._resize_pool(())

    def _respawn_dead_executors(self) -> bool:
        """Respawn any executor thread that died; return True iff one exhausted its respawn budget.

        A ``db_worker`` executor thread that hits a swallowed error (a ``DBTaskResult``
        ``OperationalError``) exits silently — the pinned queue then never drains and
        every timer chain on it freezes machine-wide while the process still looks
        healthy. Polling :meth:`is_alive` and respawning keeps the pool live; a slot
        that keeps dying past :attr:`WorkerSeams.max_respawns` is a real fault, so the
        caller exits the worker NON-ZERO instead of masking it.
        """
        for i, slot in enumerate(self._slots):
            if slot.handle.is_alive():
                continue
            if slot.respawns >= self._seams.max_respawns:
                logger.error(
                    "Executor for queue %r (slot %d) died %d times — giving up; the worker will exit non-zero.",
                    slot.queue,
                    slot.index,
                    slot.respawns,
                )
                return True
            logger.warning(
                "Executor for queue %r (slot %d) died; respawning (respawn %d).",
                slot.queue,
                slot.index,
                slot.respawns + 1,
            )
            self._slots[i] = self._spawn_slot(slot.queue, slot.index, respawns=slot.respawns + 1)
        return False

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
        self._polls_since_master_refresh += 1
        if self._polls_since_master_refresh >= self._polls_per_master_refresh():
            self._polls_since_master_refresh = 0
            self._claim_t3_master()
            self._reap_expired_leases()
        self._reclaim_dead_owner_leases()

    def run(self) -> None:
        """Reconcile, expire stale jobs, then supervise the pool against the fleet verdict until stopped."""
        seams = self._seams
        # Ownership and driving are ONE startup (#3968): the slot is claimed before the
        # chains that fire ticks exist, so `t3 loop owner` can never report "unclaimed"
        # while this process drives loops.
        self._claim_t3_master()
        seams.reconcile()
        seams.seed_chains()
        # Expire the stale `default`-queue backlog BEFORE any executor spawns, so a box
        # that queued days-old provision/ship jobs while no worker ran never blind-fires
        # them the instant the worker starts (the default-ON flip's load-jam class).
        seams.expire()

        crashed = False
        unreadable = False
        unreadable_polls = 0
        try:
            while not self._stop.is_set():
                admission = seams.read_admission()
                if admission is FleetAdmission.UNREADABLE:
                    # F7: a read FAILURE is not a deliberate stop — never a clean exit. Retry a
                    # few polls (a blip recovers), then crash so the container's restart policy restarts us.
                    unreadable_polls += 1
                    logger.warning(
                        "fleet verdict unreadable (%d/%d consecutive polls) — will crash-restart if it persists",
                        unreadable_polls,
                        seams.max_unreadable_polls,
                    )
                    seams.publish_health(admission.value, active=False)
                    if unreadable_polls >= seams.max_unreadable_polls:
                        unreadable = True
                        break
                else:
                    unreadable_polls = 0  # a recovered read resets the streak.
                    self._match_pool_to(admission)
                seams.sleep(seams.poll_seconds)
                if self._slots and self._respawn_dead_executors():
                    crashed = True
                    break
                self._per_poll_maintenance()
                if not self._stop.is_set() and admission is not FleetAdmission.UNREADABLE:
                    seams.publish_health(admission.value, active=bool(self._slots))
        finally:
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
        if stranded:
            raise LoopWorkerExecutorStopError(_STOP_MESSAGE)
        if crashed:
            raise LoopWorkerExecutorCrashError(_CRASH_MESSAGE)
        if unreadable:
            raise FleetAdmissionUnreadableError(_UNREADABLE_MESSAGE)
