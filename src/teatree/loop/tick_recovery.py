"""Boot/tick recovery sweeps and post-dispatch side-effects.

Split out of ``tick.py`` to keep the orchestrator under the module-
health LOC gate. These helpers run on either side of the dispatch
step: ``_reap_stale_task_claims`` before scanners fan out (recovering
orphaned ticket state), and the agent/mechanical helpers after dispatch
produces actions.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.loop.tick import TickReport

logger = logging.getLogger(__name__)


def _reap_stale_task_claims() -> None:
    """Recover orphaned ticket state, take over orphaned claims, reap stale ones.

    Three boot/tick recovery sweeps, ordered so a recoverable row is
    rescued before a harsher sweep can fail it. First,
    ``replay_orphaned_transitions`` (#883): a task that COMPLETED but
    whose FSM transition was lost to a mid-transition crash leaves the
    ticket half-advanced; the task is COMPLETED (not CLAIMED) so the
    claim sweeps can't see it and the loop stalls forever — this replays
    the dropped transition via the shared idempotent path. Then
    ``reclaim_orphaned_claims`` (#652): a CLAIMED task whose lease
    expired because its owning session exited mid-task is recoverable —
    returned to PENDING so another still-open session resumes it
    ("fastest open session takes over") rather than being failed.
    Finally ``reap_stale_claims``: any residual stale CLAIMED row is
    failed.

    Best-effort: if the test harness blocks DB access (pytest-django
    without a ``db`` marker), the loop tick should still render scanners
    and signals.
    """
    import contextlib  # noqa: PLC0415

    from teatree.core.models import Task  # noqa: PLC0415

    with contextlib.suppress(RuntimeError):
        Task.objects.replay_orphaned_transitions()
        Task.objects.reclaim_orphaned_claims()
        Task.objects.reap_stale_claims()


def _persist_agent_dispatches(report: "TickReport") -> None:
    """Convert ``kind="agent"`` actions into Ticket + Task DB rows.

    The DB is the dispatch queue; the ``/loop`` slot's session reads
    pending Tasks via ``t3 loop pending-spawn`` and spawns sub-agents
    in-session via its ``Agent`` tool. The statusline is purely visual
    and never an orchestration channel.

    Idempotent: if a Ticket already exists for ``(role, issue_url)`` with
    a non-completed reviewing/coding Task, no new rows are created. The
    bidirectional ``ReviewerPrsScanner`` cache (updated when the review
    Task completes) prevents re-spawning at the same SHA.
    """
    from teatree.loop.persistence import persist_agent_actions  # noqa: PLC0415

    try:
        persist_agent_actions(report.actions)
    except Exception as exc:
        logger.exception("Persisting agent dispatches failed")
        report.errors["dispatch_persist"] = f"{type(exc).__name__}: {exc}"


def _execute_mechanical(report: "TickReport") -> None:
    """Execute inline mechanical actions (ticket completions, etc.).

    Runs after dispatch but before statusline render so the statusline
    reflects the post-transition state. Errors are captured in
    ``report.errors`` — they never abort the tick.
    """
    from teatree.loop.mechanical import HANDLERS  # noqa: PLC0415

    for action in report.actions:
        if action.kind != "mechanical":
            continue
        handler = HANDLERS.get(action.zone)
        if handler is not None:
            try:
                handler(action.payload)
            except Exception as exc:
                label = f"{action.zone}[{action.payload.get('ticket_id', '?')}]"
                logger.exception("Mechanical action %s failed", label)
                report.errors[label] = f"{type(exc).__name__}: {exc}"
