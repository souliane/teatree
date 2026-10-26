"""Resolve fingerprint repair halts after their subject tickets recover."""

from collections.abc import Sequence

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import NOOP_OVER_COMPLETED_MARKER, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_subjects import SubjectIndex
from teatree.loop.repair_halt_reconcile import HALT_MARKER_PREFIX

REPAIR_HALT_RECOVERED = "repair_halt_recovered"


def recovered_repair_halts(questions: Sequence[DeferredQuestion], index: SubjectIndex) -> dict[int, str]:
    candidates: dict[int, tuple[str, tuple[int, ...]]] = {}
    for question in questions:
        if question.audience != DeferredQuestion.Audience.INTERNAL or not question.dedupe_marker.startswith(
            HALT_MARKER_PREFIX
        ):
            continue
        subjects = index.marker_tickets.get(question.dedupe_marker)
        if not subjects or any(ticket not in index.ticket_states for ticket in subjects):
            continue
        phase = normalize_phase(question.dedupe_marker.split(":", 2)[1])
        candidates[question.pk] = (phase, tuple(sorted(set(subjects))))

    if not candidates:
        return {}

    tickets = {ticket for _phase, subjects in candidates.values() for ticket in subjects}
    recovered = _recovered_lanes(tickets)
    return {
        pk: f"every ticket that raised the halt is settled or has passed {phase} again ({count} recovered)"
        for pk, (phase, subjects) in candidates.items()
        if (count := sum((ticket, phase) in recovered for ticket in subjects))
        and all(
            (ticket, phase) in recovered or Ticket(state=index.ticket_states[ticket]).is_settled for ticket in subjects
        )
    }


def _recovered_lanes(tickets: set[int]) -> set[tuple[int, str]]:
    latest_failure: dict[tuple[int, str], int] = {}
    possible_successes: list[tuple[int, tuple[int, str], int]] = []
    attempts = TaskAttempt.objects.filter(
        task__ticket_id__in=tickets,
        outcome__in=(TaskAttempt.Outcome.SUCCESS, *TaskAttempt.FAILED_OUTCOMES),
    ).values_list("pk", "task__ticket_id", "task__phase", "task__status", "task_id", "outcome", "result")
    for pk, ticket, phase, status, task_id, outcome, result in attempts:
        lane = (ticket, normalize_phase(phase))
        if outcome in TaskAttempt.FAILED_OUTCOMES:
            latest_failure[lane] = max(pk, latest_failure.get(lane, 0))
            continue
        if status != Task.Status.COMPLETED:
            continue
        result_data = result if isinstance(result, dict) else {}
        if result_data.get("needs_user_input") or NOOP_OVER_COMPLETED_MARKER in str(result_data.get("summary") or ""):
            continue
        possible_successes.append((pk, lane, task_id))

    parked = set(
        DeferredQuestion.pending()
        .filter(parked_task_id__in={task_id for _pk, _lane, task_id in possible_successes})
        .values_list("parked_task_id", flat=True)
    )
    latest_success: dict[tuple[int, str], int] = {}
    for pk, lane, task_id in possible_successes:
        if task_id not in parked:
            latest_success[lane] = max(pk, latest_success.get(lane, 0))

    return {lane for lane, success in latest_success.items() if success > latest_failure.get(lane, 0)}
