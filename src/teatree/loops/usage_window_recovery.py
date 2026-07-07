"""The self-rescheduling usage-window re-arm chain — idle auto-recovery (Directive #3).

When a Claude usage window empties, ``teatree.agents.usage_window`` PARKS the affected
tasks (a :class:`~teatree.core.models.UsageWindowState` row per lane + a ``not_before`` on
each task) instead of failing them and going idle forever. This module is the re-arm arm:
a single self-rescheduling ``usage_window_recovery`` loop-timer job on the existing
``LOOPS_QUEUE`` (the #10 PR-04 machinery, NO OS cron) that, once the reset instant passes,
CLEARS the window, RELEASES the parked tasks, PUMPS the loop so they get re-claimed, and
posts ONE Slack line. It is the deterministic, zero-inference twin of the loop-timer /
maintenance chains (``reconcile_timers`` / ``prune_task_results``): the re-arm decision is
purely time-based (``resets_at`` has passed), so a released task that re-hits the limit
simply re-parks — no fragile "is the model reachable yet" inference probe is needed.

Gated by the DARK ``limit_autorecovery_enabled`` flag: while it is OFF the chain is a cheap
keepalive that clears nothing, releases nothing, and posts nothing (behaviorally inert), so
the whole feature ships dark. Seeded by ``ensure_maintenance_chains`` at worker startup and
self-perpetuating after that.

Harness-limited (NOT teatree-fixable, stated plainly): this re-arms the WORKER / DB loop
plane. Resuming the interactive orchestrator SESSION's conversation, or auto-resuming the
Claude Code harness's own session, at reset is outside teatree — there is no supported API
to inject a prompt into a live interactive session. The mitigation is architectural: keep
moving loop ownership to this worker/DB plane so the interactive session is optional.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field

from django.db.models import F
from django.tasks import task
from django.utils import timezone

from teatree.loops.timer_chains import LOOPS_QUEUE

logger = logging.getLogger(__name__)

#: While a window is still parked the chain polls at this cadence so recovery lands within
#: ~a minute of the reset; when no window is active it idles at the slower keepalive cadence
#: (still alive to react to a flag flip + a fresh park).
ACTIVE_POLL_SECONDS = 60
IDLE_KEEPALIVE_SECONDS = 300


@dataclass(frozen=True)
class RecoveryOutcome:
    """What one recovery pass did — the cleared window pks and the released-task count."""

    cleared: list[int] = field(default_factory=list)
    released: int = 0


def recover_windows(now: dt.datetime) -> RecoveryOutcome:
    """Clear every window whose reset has passed; release + pump + notify if any cleared.

    Deterministic and idempotent: a window clears exactly once ``now`` reaches its
    ``resets_at`` (a null-reset API-credit window never clears here — it has no time-based
    recovery). A still-parked window's ``probe_count`` is bumped for diagnostics. When at
    least one window cleared, the parked tasks whose gate has elapsed are released
    (``not_before`` nulled → immediately claimable), the loops are pumped so they re-claim,
    and ONE Slack line is posted.
    """
    from teatree.core.models import UsageWindowState  # noqa: PLC0415 — deferred import (cycle-safe / task-body)

    active = list(UsageWindowState.objects.active())
    cleared: list[int] = []
    still_down: list[int] = []
    for window in active:
        if window.should_clear(now):
            window.clear(now)
            cleared.append(window.pk)
        else:
            still_down.append(window.pk)
    if still_down:
        UsageWindowState.objects.filter(pk__in=still_down).update(probe_count=F("probe_count") + 1)
    if not cleared:
        return RecoveryOutcome()

    released = _release_parked_tasks(now)
    _wake_loops(now)
    _notify_recovered(cleared_pks=cleared, released=released, now=now)
    logger.info("usage_window_recovery: cleared %s window(s), released %s parked task(s)", len(cleared), released)
    return RecoveryOutcome(cleared=cleared, released=released)


def _release_parked_tasks(now: dt.datetime) -> int:
    """Null the ``not_before`` of PENDING tasks whose park gate has elapsed. Returns the count.

    A parked task with ``not_before <= now`` is already claimable; nulling it makes the
    release explicit and immediate (and gives the notify a meaningful count). A task still
    parked behind a not-yet-due window (``not_before`` in the future) is untouched.
    """
    from teatree.core.models import Task  # noqa: PLC0415 — deferred import (cycle-safe / task-body)

    return Task.objects.filter(
        status=Task.Status.PENDING,
        not_before__isnull=False,
        not_before__lte=now,
    ).update(not_before=None)


def _wake_loops(now: dt.datetime) -> None:
    """Pump every enabled loop's timer to fire now so the released tasks get re-claimed.

    Best-effort — a pump failure must never break recovery; the released tasks are picked up
    on the next ordinary loop tick regardless (the idle poll floor caps the latency).
    """
    try:
        from teatree.loops.timer_chains import refine_successor  # noqa: PLC0415 — deferred (cycle-safe)
        from teatree.loops.timer_reconciler import timer_chain_loop_names  # noqa: PLC0415 — deferred (cycle-safe)

        for name in timer_chain_loop_names():
            refine_successor(name, run_after=now)
    except Exception:
        logger.debug("usage_window_recovery loop pump failed", exc_info=True)


def _notify_recovered(*, cleared_pks: list[int], released: int, now: dt.datetime) -> None:
    """Post ONE bot→user Slack line that the window(s) recovered — no-op-safe, never raises."""
    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415 — deferred import (cycle-safe / task-body)

    key = "usage_window_recovered:" + "-".join(str(pk) for pk in sorted(cleared_pks))
    text = (
        f"Usage window restored {now:%H:%M} — cleared {len(cleared_pks)} window(s), "
        f"released {released} parked task(s), resumed the loop."
    )
    try:
        notify_user(text, kind=NotifyKind.INFO, idempotency_key=key)
    except Exception:
        logger.debug("usage_window_recovery notify failed for key=%s", key, exc_info=True)


def _autorecovery_enabled() -> bool:
    """Whether ``limit_autorecovery_enabled`` resolves ON — fail-safe OFF (dark by default)."""
    try:
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred import (cycle-safe / task-body)

        return bool(get_effective_settings().limit_autorecovery_enabled)
    except Exception:
        logger.debug("limit_autorecovery_enabled read failed — treating auto-recovery as OFF", exc_info=True)
        return False


def _pending_recovery() -> bool:
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred import (cycle-safe / task-body)
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred import (cycle-safe / task-body)

    return DBTaskResult.objects.filter(
        task_path=usage_window_recovery.module_path,
        status=TaskResultStatus.READY,
    ).exists()


def _next_fire(now: dt.datetime) -> dt.datetime:
    """The next recovery fire — fast poll while a window is parked, slow keepalive otherwise."""
    from teatree.core.models import UsageWindowState  # noqa: PLC0415 — deferred import (cycle-safe / task-body)

    cadence = ACTIVE_POLL_SECONDS if UsageWindowState.objects.active().exists() else IDLE_KEEPALIVE_SECONDS
    return now + dt.timedelta(seconds=cadence)


@task(queue_name=LOOPS_QUEUE)
def usage_window_recovery() -> dict[str, int]:
    """One recovery fire: clear due windows, then re-schedule this chain.

    Self-dedups first (another pending recovery carries the chain), mirroring the
    ``reconcile_timers`` contract, so an at-least-once redelivery collapses to one. While
    ``limit_autorecovery_enabled`` is OFF the body is a cheap keepalive — it clears nothing
    and posts nothing (behaviorally inert) but keeps re-scheduling so a flag flip is picked
    up without a worker restart.
    """
    if _pending_recovery():
        return {"deduped": 1}
    now = timezone.now()
    if not _autorecovery_enabled():
        usage_window_recovery.using(run_after=now + dt.timedelta(seconds=IDLE_KEEPALIVE_SECONDS)).enqueue()
        return {"disabled": 1}
    outcome = recover_windows(now)
    usage_window_recovery.using(run_after=_next_fire(timezone.now())).enqueue()
    return {"cleared": len(outcome.cleared), "released": outcome.released}


def ensure_usage_window_recovery_chain() -> None:
    """Seed the recovery chain head if absent — self-perpetuating after that (worker startup)."""
    if not _pending_recovery():
        usage_window_recovery.using(run_after=timezone.now() + dt.timedelta(seconds=ACTIVE_POLL_SECONDS)).enqueue()
