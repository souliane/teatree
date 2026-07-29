"""Phases the headless worker executes deterministically, never as a generic agent spawn.

Most headless phases are agentic: the worker builds a ticket-work brief and drives a
model through it. A few are not — they are fixed data transformations that happen to be
scheduled as ``Task`` rows so a loop scanner does not have to run an LLM inline
(``no synchronous LLM in scan()``). Dispatching one of those through the agentic path
hands it the generic *"Work on ticket N — check git log, code, test, run
``t3 tool verify-gates``"* brief, which contradicts its least-privilege toolset: the
brief demands the shell the phase is (correctly) denied, so the agent follows its own
stop-rule, records ``needs_user_input`` and parks. The scanner's dedup filter ignores
FAILED, so the next tick re-enqueues it — a retry storm of unanswerable questions.

Both headless entry points consult this registry — the ``tasks work-next-headless``
command AND the django-tasks worker ``core.tasks.execute_headless_task`` (#3570: that
second lane went straight to the agentic runner, so every ``short_describe`` task
completed exit-0 with the agent NARRATING a summary it had no path to persist, and the
scanner's COMPLETED dedup then suppressed the ticket forever).

It lives in the domain layer because both callers must reach it, and registration is
INVERTED (mirroring :mod:`teatree.core.headless_dispatch`): a runner that needs the
model seam lives in ``teatree.agents`` and registers itself at app-ready, so ``core``
never imports the higher layer.
"""

import logging
import traceback
from collections.abc import Callable

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task

logger = logging.getLogger(__name__)

#: A runner takes the claimed task and returns the human-readable outcome line(s) it
#: wants recorded on the ``TaskAttempt``. Raising is fine — the caller records the
#: traceback as a failed attempt, the same contract the agentic path has.
PhaseRunner = Callable[[Task], str]

_RUNNERS: dict[str, PhaseRunner] = {}


def register_phase_runner(phase: str, runner: PhaseRunner) -> None:
    """Register *runner* as the deterministic implementation of *phase*."""
    _RUNNERS[normalize_phase(phase)] = runner


def deterministic_phase_runner(phase: str) -> PhaseRunner | None:
    """The deterministic runner for *phase*, or ``None`` when it dispatches agentically."""
    return _RUNNERS.get(normalize_phase(phase))


def run_deterministic_phase(task: Task) -> dict[str, str] | None:
    """Execute *task* deterministically when its phase is non-agentic, else ``None``.

    Failures are recorded through the same durable recorder as the agentic path, so a
    raise never leaves the task stuck CLAIMED. ``None`` means dispatch agentically.
    """
    runner = deterministic_phase_runner(task.phase)
    if runner is None:
        return None
    try:
        outcome = runner(task)
    except Exception:  # noqa: BLE001 — a deterministic-phase failure is recorded durably, never escapes.
        error = traceback.format_exc()
        logger.warning("Task %s: deterministic phase %r raised", task.pk, task.phase)
        task.complete_with_attempt(exit_code=1, error=error, result={"phase_error": error})
        return {"exit_code": "1", "phase_error": error}
    attempt = task.complete_with_attempt(exit_code=0, result={"summary": outcome})
    return {"exit_code": "0", "attempt_id": str(attempt.pk)}


__all__ = [
    "PhaseRunner",
    "deterministic_phase_runner",
    "register_phase_runner",
    "run_deterministic_phase",
]
