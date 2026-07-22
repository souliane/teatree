"""Phases the headless worker executes deterministically, never as a generic agent spawn.

Most headless phases are agentic: the worker builds a ticket-work brief and drives a
model through it. A few are not — they are fixed data transformations scheduled as
``Task`` rows so a loop scanner does not run an LLM inline (``no synchronous LLM in
scan()``). Dispatching one of those agentically hands it the generic *"Work on ticket
N — check git log, code, test, run ``t3 tool verify-gates``"* brief, which contradicts
its least-privilege toolset: the brief demands the shell the phase is (correctly)
denied, so the agent parks with ``needs_user_input`` and the scanner (whose dedup filter
ignores FAILED) re-enqueues it — a retry storm of unanswerable questions.

A phase's runner is registered here by the owning higher layer at app-ready (e.g.
``teatree.agents.short_describe.run_short_describe`` via ``teatree.agents.apps``), so
BOTH headless dispatch chokepoints route the phase to its own implementation through
this single seam: the ``core.tasks.execute_headless_task`` ``@task`` worker (the
scanner's auto-enqueue / drain lane) and the ``work-next-headless`` CLI. The seam lives
in ``teatree.core`` so the domain ``@task`` worker can consult it without an inverted
dependency on the interface/agents layers — the same core→agents inversion
``teatree.core.headless_dispatch`` uses for the agentic runner.
"""

import logging
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING

from teatree.core.modelkit.phases import normalize_phase

if TYPE_CHECKING:
    from teatree.core.models import Task

logger = logging.getLogger(__name__)

#: A runner takes the claimed task and returns the human-readable outcome line(s) it
#: wants recorded on the ``TaskAttempt``. Raising is fine — the caller records the
#: traceback as a failed attempt, the same contract the agentic path has.
DeterministicPhaseRunner = Callable[["Task"], str]

_runners: dict[str, DeterministicPhaseRunner] = {}


def register_deterministic_phase(phase: str, runner: DeterministicPhaseRunner) -> None:
    """Register *runner* as the deterministic implementation of *phase* (matched normalized)."""
    _runners[normalize_phase(phase)] = runner


def deterministic_phase_runner(phase: str) -> DeterministicPhaseRunner | None:
    """The deterministic runner for *phase*, or ``None`` when it dispatches agentically."""
    return _runners.get(normalize_phase(phase))


def run_deterministic_phase(task: "Task") -> dict[str, str] | None:
    """Execute *task* deterministically when its phase is non-agentic, else ``None``.

    A non-agentic phase runs its registered implementation rather than a generic
    ticket-work brief its least-privilege toolset cannot satisfy. Failures are recorded
    through the same durable recorder as the agentic path, so a raise never leaves the
    task stuck CLAIMED. ``None`` means the phase dispatches agentically.
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
    "DeterministicPhaseRunner",
    "deterministic_phase_runner",
    "register_deterministic_phase",
    "run_deterministic_phase",
]
