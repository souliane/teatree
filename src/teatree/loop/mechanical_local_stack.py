"""Mechanical handlers for the idle reaper + queue drainer (souliane/teatree#2190, #44).

Split out of :mod:`teatree.loop.mechanical` so the stop/start logic lives in
one self-describing module and ``mechanical.py`` only registers the entry
points in ``HANDLERS``.

``reap_idle_stack`` — the executor for ``local_stack.reap_idle``: re-verify the
worktree is STILL idle+reapable against the live DB (fail-CLOSED stale-read
guard — a stack revived between scan and handler is kept), then fire
``Worktree.stop_services`` under a row lock guarded by ``can_proceed``. The
demotion is REVERSIBLE (DB + worktree preserved) and the on_commit worker
brings the WHOLE compose project down so no stray container survives.

``drain_stack_queue_item`` — the executor for ``local_stack.queue_acquire``:
re-check the per-overlay cap for the queued worktree; on a free slot fire
``start_services`` and mark the item READY; on a still-full cap reschedule the
next Fibonacci attempt. It only ever advances its OWN worktree's FSM — it
never tears down another ticket's stack.

Both handlers are best-effort and idempotent: a missing/terminal row, a stale
read, or a DB error logs and returns rather than crashing the tick.
"""

import logging

from django.db import transaction
from django_fsm import can_proceed

from teatree.config import get_effective_settings
from teatree.core.gates.idle_stack import reapable_worktrees
from teatree.core.gates.local_stack_gate import (
    LocalStackLimitExceededError,
    check_local_stack_limit,
    resolve_max_concurrent_local_stacks,
)
from teatree.core.gates.provision_admission_gate import check_provision_admission
from teatree.core.models import LocalStackQueueItem, Worktree
from teatree.loop.dispatch import ActionPayload

logger = logging.getLogger(__name__)

_TERMINAL_QUEUE_STATES = frozenset(
    {LocalStackQueueItem.Status.READY, LocalStackQueueItem.Status.DONE, LocalStackQueueItem.Status.DEAD},
)


def reap_idle_stack(payload: ActionPayload) -> None:
    """Stop a still-idle running worktree's stack → demote to PROVISIONED (#2190).

    Re-verifies the worktree is STILL in the reapable set against the live DB
    (the scanner flagged it a tick ago; an intervening ``start``/session could
    have revived it). Only if it is still reapable does it fire
    ``stop_services`` under a row lock guarded by ``can_proceed`` — so a
    concurrent transition between the re-verify and the stop never raises.
    """
    worktree_id = payload.get("worktree_id")
    if worktree_id is None:
        return
    overlay = str(payload.get("overlay", ""))
    idle_minutes = int(get_effective_settings().idle_stack_idle_minutes)
    still_reapable = {wt.pk for wt in reapable_worktrees(overlay=overlay, idle_minutes=idle_minutes)}
    if worktree_id not in still_reapable:
        logger.info("reap_idle_stack: worktree %s no longer reapable — keeping (fail-safe)", worktree_id)
        return
    with transaction.atomic():
        try:
            worktree = Worktree.objects.select_for_update().select_related("ticket").get(pk=worktree_id)
        except Worktree.DoesNotExist:
            return
        if not can_proceed(worktree.stop_services):
            logger.info("reap_idle_stack: stop_services not allowed for %s (state=%s)", worktree_id, worktree.state)
            return
        worktree.stop_services()
        worktree.save()
    logger.info("reap_idle_stack: demoted idle worktree %s to provisioned (slot freed)", worktree_id)


def drain_stack_queue_item(payload: ActionPayload) -> None:
    """Re-check the cap for a queued acquisition; start it or back it off (#2190, #44).

    On a free slot: fire ``Worktree.start_services`` and mark the queue item
    READY. On a still-full cap: reschedule the next Fibonacci attempt (or mark
    DEAD once ``local_stack_queue_max_attempts`` is exhausted). It only ever
    advances the queued worktree's OWN FSM — never another ticket's stack.
    """
    item_id = payload.get("queue_item_id")
    if item_id is None:
        return
    with transaction.atomic():
        try:
            item = LocalStackQueueItem.objects.select_for_update().select_related("worktree__ticket").get(pk=item_id)
        except LocalStackQueueItem.DoesNotExist:
            return
        if item.status in _TERMINAL_QUEUE_STATES:
            return
        worktree = item.worktree
        max_attempts = int(get_effective_settings().local_stack_queue_max_attempts)
        limit = resolve_max_concurrent_local_stacks()
        # #2949: resource-aware admission — on a capped overlay, hold the drain
        # while host RAM is over the ceiling even if a count slot is free, so
        # draining the queue never itself pushes the host into OOM. Same durable
        # backoff as a full cap. An unbounded overlay (limit ``0``) is untouched.
        if limit > 0:
            verdict = check_provision_admission()
            if not verdict.ok:
                item.schedule_next_attempt(error=f"ram_pressure: {verdict.reason}"[:500], max_attempts=max_attempts)
                logger.info(
                    "drain_stack_queue_item: RAM over ceiling — backoff for item %s (attempt %s)",
                    item_id,
                    item.attempt_count,
                )
                return
        try:
            check_local_stack_limit(worktree, limit=limit)
        except LocalStackLimitExceededError as exc:
            item.schedule_next_attempt(error=str(exc)[:500], max_attempts=max_attempts)
            logger.info(
                "drain_stack_queue_item: still full — backoff for item %s (attempt %s)",
                item_id,
                item.attempt_count,
            )
            return
        if not can_proceed(worktree.start_services):
            logger.info("drain_stack_queue_item: start_services not allowed for worktree %s", worktree.pk)
            return
        commands = _run_commands(worktree)
        worktree.start_services(services=commands)
        worktree.save()
        item.mark_ready()
    logger.info("drain_stack_queue_item: slot freed — started worktree %s from queue item %s", worktree.pk, item_id)


def _run_commands(worktree: Worktree) -> list[str] | None:
    """Resolve the overlay's run commands for *worktree*, or ``None`` on any failure.

    Best-effort: an unresolvable overlay must not wedge the drainer — the
    start proceeds with the default service set (``None``) so the worker
    enumerates them itself.
    """
    from teatree.core.overlay_loader import get_overlay_for_worktree  # noqa: PLC0415 — deferred: loaded at tick time

    try:
        overlay = get_overlay_for_worktree(worktree)
        return list(overlay.runtime.run_commands(worktree))
    except Exception:
        logger.exception("drain_stack_queue_item: could not resolve run commands for worktree %s", worktree.pk)
        return None


__all__ = ["drain_stack_queue_item", "reap_idle_stack"]
