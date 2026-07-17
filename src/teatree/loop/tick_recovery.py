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
    from collections.abc import Callable

    from teatree.loop.tick import TickReport

logger = logging.getLogger(__name__)


def _reap_stale_task_claims(errors: dict[str, str] | None = None) -> None:
    """Run the three recovery sweeps INDEPENDENTLY, recording each failure — never silently.

    Chains :func:`teatree.core.worktree.recovery_sweeps.run_boot_sweeps` (the single
    SSOT, shared with ``t3 recover``), :func:`~teatree.loop.transient_requeue.requeue_transient_failed`
    (the bounded reopen of transient-FAILED tasks a crashed-session boot sweep never
    rescues), and :func:`~teatree.loop.stuck_ticket_redispatch.redispatch_stuck_tickets`
    (the bounded re-dispatch of stuck non-terminal tickets). The two loop-layer sweeps
    compose the ``agents``/``core`` surfaces, so they run here rather than in the
    core-only ``run_boot_sweeps``.

    Each sweep runs in its OWN ``try`` so a ``RuntimeError`` from the FIRST never skips
    the other two (the old shared ``suppress(RuntimeError)`` let one boot-sweep failure
    silently disable transient-requeue AND stuck-redispatch, and recovery itself failed
    invisibly). A failure is logged and recorded in *errors* (rendered in the tick's
    ``action_needed``), so a DB-blocked pytest-django harness still renders while a real
    recovery failure surfaces loudly instead of freezing the factory in silence.
    """
    from teatree.core.worktree.recovery_sweeps import run_boot_sweeps  # noqa: PLC0415 — deferred: loaded at tick time
    from teatree.loop import stuck_ticket_redispatch, transient_requeue  # noqa: PLC0415 — deferred: loaded at tick time

    sweeps: tuple[tuple[str, Callable[[], object]], ...] = (
        ("recovery:boot_sweeps", run_boot_sweeps),
        ("recovery:transient_requeue", transient_requeue.requeue_transient_failed),
        ("recovery:stuck_redispatch", stuck_ticket_redispatch.redispatch_stuck_tickets),
    )
    for label, sweep in sweeps:
        try:
            sweep()
        except RuntimeError as exc:
            logger.warning("Recovery sweep %s failed: %s", label, exc)
            if errors is not None:
                errors[label] = f"{type(exc).__name__}: {exc}"


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
    from teatree.loop.persistence import persist_agent_actions  # noqa: PLC0415 — deferred: loaded at tick time

    try:
        # Thread the report's error sink so a dropped/failed per-zone persist
        # records ``errors["persist:<zone>"]`` (rendered in action_needed) rather
        # than a silent ``logger.debug`` — the #1 blocker fail-loud contract.
        persist_agent_actions(report.actions, errors=report.errors)
    except Exception as exc:
        logger.exception("Persisting agent dispatches failed")
        report.errors["dispatch_persist"] = f"{type(exc).__name__}: {exc}"


def _execute_mechanical(report: "TickReport") -> None:
    """Execute inline mechanical actions (ticket completions, etc.).

    Runs after dispatch but before statusline render so the statusline
    reflects the post-transition state. Errors are captured in
    ``report.errors`` — they never abort the tick.
    """
    from teatree.loop.mechanical import HANDLERS  # noqa: PLC0415 — deferred: loaded at tick time, not import

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
