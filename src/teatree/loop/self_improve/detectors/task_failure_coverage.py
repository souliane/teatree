"""Check whether a finished repair ticket covers every surviving failed task."""

from teatree.core.models import SelfImproveFiring, TaskAttempt, Ticket, TicketTransition


def failures_handled_by_finished_repair(firing: SelfImproveFiring | None, ids: list[int]) -> bool:
    if firing is None or firing.resolved_at is not None or not ids:
        return False
    ticket = firing.ticket
    finished_states = Ticket.finished_states()
    if ticket is None or ticket.state not in finished_states:
        return False
    finished_at = None
    transitions = TicketTransition.objects.filter(ticket_id=ticket.pk).order_by("-created_at", "-pk")
    for state, created_at in transitions.values_list("to_state", "created_at").iterator():
        if state not in finished_states:
            break
        finished_at = created_at
    if finished_at is None:
        return False
    seen: set[int] = set()
    for task_id, ended_at in TaskAttempt.objects.filter(task_id__in=ids).values_list("task_id", "ended_at").iterator():
        if ended_at is None or ended_at > finished_at:
            return False
        seen.add(task_id)
    return seen == set(ids)
