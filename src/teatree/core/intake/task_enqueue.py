"""Manual phase enqueue with the intake facts a planner must receive."""

from teatree.core.intake.landscape_persist import ensure_planning_landscape
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.task import Task
from teatree.core.models.task_enqueue import TaskEnqueueError
from teatree.core.models.task_enqueue import enqueue_phase_task as _create_phase_task
from teatree.core.models.task_enqueue import enqueue_phase_task_once as _create_phase_task_once
from teatree.core.models.ticket import Ticket


def _ensure_planning_survey(ticket: Ticket, phase: str, reason: str) -> None:
    if normalize_phase(phase) != "planning" or not reason.strip():
        return
    try:
        ensure_planning_landscape(ticket)
    except Exception as exc:
        msg = f"cannot queue planning without an intake landscape survey: {exc}"
        raise TaskEnqueueError(msg) from exc


def enqueue_phase_task(*, ticket: Ticket, phase: str, reason: str, agent_id: str = "phase-handoff") -> Task:
    _ensure_planning_survey(ticket, phase, reason)
    return _create_phase_task(ticket=ticket, phase=phase, reason=reason, agent_id=agent_id)


def enqueue_phase_task_once(*, ticket: Ticket, phase: str, reason: str, agent_id: str = "dashboard") -> Task:
    _ensure_planning_survey(ticket, phase, reason)
    return _create_phase_task_once(ticket=ticket, phase=phase, reason=reason, agent_id=agent_id)
