"""Self-owned singleton loop-runner daemon — the driver that owns the tick cadence (#2876).

Replaces the native Claude ``/loop`` cron as the *cadence owner* for DB ``Loop``
rows. WHAT a tick does is unchanged — the beat only decides WHEN. Each beat it
asks the unchanged unified verdict (:func:`teatree.loops.loop_table.admitted_loop_names`)
which rows are enabled + due and enqueues one :func:`teatree.core.tasks.execute_loop`
task per admitted row onto the dedicated ``loop-runner`` django-tasks queue; a
batch drain of that queue then runs each per-loop tick out-of-band. The per-loop
tick's ``mark_run_if_unchanged`` CAS is the sole idempotency guard, so an
at-least-once double delivery is a no-op — the beat never claims a cadence anchor.

Two supervision layers deliver *at least one* runner without any OS scheduler
(no cron / launchd / systemd): the flock singleton
(:func:`teatree.utils.singleton.singleton`) gives *at most one*, the supervisor
loop here respawns a crashed beat worker, and the SessionStart resurrector
(:mod:`hooks.scripts.loop_runner_supervisor`) re-spawns the whole daemon when the
flock is free. On a fully-headless box the operator starts ``t3 loop-runner`` once
from a login profile — a dotfile, not a system scheduler.

The runtime transport (the Pydantic-AI harness + OpenAI-compatible router) and
cached-resume are owned by epic #2565; this module keeps the existing Claude-SDK
dispatch path unchanged.
"""

import datetime as dt
import logging
import os
import time
from collections.abc import Callable

from django.utils import timezone

logger = logging.getLogger(__name__)

#: The flock singleton name (:mod:`teatree.utils.singleton`) the daemon holds so
#: at most one loop-runner exists per box.
LOOP_RUNNER_SINGLETON = "loop-runner"

#: Decision-1 beat clamp: the coarse beat never exceeds this ceiling …
_BEAT_CEILING_SECONDS = 30.0
#: … and never drops below this floor (avoids a busy spin on a fast loop).
_BEAT_FLOOR_SECONDS = 5.0
#: Backoff after the supervisor catches a crashed beat worker, before respawn.
_RESPAWN_BACKOFF_SECONDS = 1.0
#: A single drain runs at most this many queued ticks, so a runaway queue can
#: never block the beat forever — the next beat continues where it left off.
_DRAIN_BATCH_CAP = 200


def compute_beat_seconds() -> float:
    """The coarse beat interval (#2876 decision 1): ``max(5, min(30, min_delay / 2))``.

    Clamped to half the shortest ENABLED interval ``delay_seconds`` so the tightest
    interval cadence is never missed, capped at a 30s ceiling and floored at 5s to
    avoid a busy spin. Daily-only (``daily_at``) loops do not lower the beat, and an
    every-tick (no-cadence) loop contributes no ``delay_seconds`` — with no enabled
    interval loop the beat sits at the 30s ceiling.
    """
    from teatree.core.models import Loop  # noqa: PLC0415

    delays = [
        row.delay_seconds for row in Loop.objects.enabled() if row.delay_seconds is not None and row.daily_at is None
    ]
    if not delays:
        return _BEAT_CEILING_SECONDS
    return max(_BEAT_FLOOR_SECONDS, min(_BEAT_CEILING_SECONDS, min(delays) / 2))


def enqueue_due_loops(now: dt.datetime | None = None) -> list[str]:
    """Enqueue one ``execute_loop`` task per admitted row — the beat body (#2876).

    Pure DB reads plus an enqueue: it asks the unchanged unified verdict which rows
    are enabled + due (NO model call, NO cadence claim) and enqueues a per-loop tick
    for each. A silent beat — no admitted row — enqueues NOTHING and therefore
    dispatches no model. Returns the admitted names for observability / tests.
    """
    from teatree.core.tasks import execute_loop  # noqa: PLC0415
    from teatree.loops.loop_table import admitted_loop_names  # noqa: PLC0415

    names = admitted_loop_names(now or timezone.now())
    for name in names:
        execute_loop.enqueue(name)
    return names


def drain_loop_queue() -> None:
    """Drain outstanding ``loop-runner``-queue ticks in-process (bounded batch).

    Reuses django-tasks' own batch :class:`Worker` scoped to the dedicated
    ``loop-runner`` queue, so a per-loop tick never blocks behind a heavy
    default-queue FSM/headless job, and the atomic row claim means a concurrent
    ``db_worker`` on ``*`` can never double-run the same tick. Bounded by
    ``_DRAIN_BATCH_CAP`` per call; batch mode returns as soon as the queue is empty.
    """
    from django_tasks import DEFAULT_TASK_BACKEND_ALIAS  # noqa: PLC0415
    from django_tasks.utils import get_random_id  # noqa: PLC0415
    from django_tasks_db.management.commands.db_worker import Worker  # noqa: PLC0415

    from teatree.core.tasks import LOOP_RUNNER_QUEUE  # noqa: PLC0415

    Worker(
        queue_names=[LOOP_RUNNER_QUEUE],
        interval=0.0,
        batch=True,
        backend_name=DEFAULT_TASK_BACKEND_ALIAS,
        startup_delay=False,
        max_tasks=_DRAIN_BATCH_CAP,
        worker_id=f"loop-runner-{os.getpid()}-{get_random_id()}",
    ).run()


def _never() -> bool:
    return False


class LoopRunnerDaemon:
    """Supervised beat daemon: respawn the beat worker on crash, drain each beat (#2876).

    The collaborators (``beat`` / ``drain`` / ``beat_seconds`` / ``sleep`` /
    ``stop``) are injected so the supervision and cadence logic are tested without a
    real clock or a real queue. The defaults wire the production seams.
    """

    def __init__(
        self,
        *,
        beat: Callable[[], object] | None = None,
        drain: Callable[[], object] | None = None,
        beat_seconds: Callable[[], float] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        self._beat = beat or enqueue_due_loops
        self._drain = drain or drain_loop_queue
        self._beat_seconds = beat_seconds or compute_beat_seconds
        self._sleep = sleep
        self._stop = stop or _never

    def run(self) -> None:
        """Supervise the beat worker forever, respawning it whenever it crashes.

        Only a bug (``Exception``) in the beat worker triggers a respawn; a
        ``KeyboardInterrupt`` / ``SystemExit`` propagates so a signal stops the
        daemon cleanly — the flock releases on process death regardless, so
        at-most-one is never left stale.
        """
        while not self._stop():
            try:
                self._beat_worker()
            except Exception:
                logger.exception("loop-runner beat worker crashed — respawning")
                self._sleep(_RESPAWN_BACKOFF_SECONDS)
            else:
                return

    def run_once(self) -> None:
        """Run a single beat + drain and return — the foreground / test variant.

        The ``t3 loop-runner --once`` variant that supersedes the removed foreground
        ``loops run`` runner (#2880): no supervisor, no sleep, no respawn.
        """
        self._beat()
        self._drain()

    def _beat_worker(self) -> None:
        while not self._stop():
            self._beat()
            self._drain()
            self._sleep(self._beat_seconds())
