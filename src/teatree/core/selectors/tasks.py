from django.db.models import Min

from teatree.core.models import Task, Ticket, TicketTransition

from ._types import TaskAttemptDetail, TaskDetail, TaskRelatedRow


def build_task_detail(task_id: int) -> TaskDetail | None:
    task = Task.objects.filter(pk=task_id).select_related("session", "ticket", "parent_task").first()
    if task is None:
        return None

    parent = None
    if task.parent_task_id:
        p = task.parent_task
        parent = TaskRelatedRow(
            task_id=p.pk,
            phase=p.phase,
            status=p.get_status_display(),
            execution_reason=p.execution_reason[:120],
        )

    children = [
        TaskRelatedRow(
            task_id=c.pk,
            phase=c.phase,
            status=c.get_status_display(),
            execution_reason=c.execution_reason[:120],
        )
        for c in task.child_tasks.order_by("pk")
    ]

    attempts = [
        TaskAttemptDetail(
            attempt_id=a.pk,
            started_at=a.started_at.isoformat() if a.started_at else "",
            ended_at=a.ended_at.isoformat() if a.ended_at else "",
            exit_code=a.exit_code,
            error=a.error,
            result=a.result if isinstance(a.result, dict) else {},
            agent_session_id=a.agent_session_id,
        )
        for a in task.attempts.order_by("-pk")
    ]

    return TaskDetail(
        task_id=task.pk,
        ticket_id=task.ticket_id,
        ticket_display_id=task.ticket.ticket_number,
        phase=task.phase,
        status=task.get_status_display(),
        execution_reason=task.execution_reason,
        claimed_by=task.claimed_by,
        session_agent_id=task.session.agent_id if task.session_id else "",
        parent=parent,
        children=children,
        attempts=attempts,
    )


def build_ticket_lifecycle_mermaid(ticket_id: int) -> str:
    """Build a Mermaid stateDiagram-v2 from the DISTINCT edges the ticket recorded.

    A state diagram is a set of edges, so a repair loop that re-ran one transition
    22,965 times must still draw one arrow — the deployed box's worst ticket holds
    exactly that many rows across ten distinct edges. Grouping in SQL keeps both the
    query result and the rendered diagram bounded by the FSM's own transition space
    instead of by how long the ticket has been worked.
    """
    ticket = Ticket.objects.get(pk=ticket_id)
    edges = (
        TicketTransition.objects.filter(ticket_id=ticket_id)
        .values("from_state", "to_state", "triggered_by", "session_id")
        .annotate(first_seen=Min("created_at"))
        .order_by("first_seen")
    )

    lines = ["stateDiagram-v2", f"    [*] --> {ticket.State.NOT_STARTED}"]
    referenced: set[str] = {ticket.State.NOT_STARTED}

    for edge in edges:
        label = f"{edge['triggered_by']}()"
        if edge["session_id"]:
            label += f" S{edge['session_id']}"
        lines.append(f"    {edge['from_state']} --> {edge['to_state']}: {label}")
        referenced.update((edge["from_state"], edge["to_state"]))

    # mermaid v11 stateDiagram-v2 requires every state in a ``note`` to be defined
    # by an edge. Without recorded transitions a ticket whose state is not the
    # initial NOT_STARTED would dangle the note → render fails with
    # "No such shape: undefined".
    if ticket.state not in referenced:
        lines.append(f"    {ticket.State.NOT_STARTED} --> {ticket.state}")

    lines.append(f"    note right of {ticket.state}: current")

    return "\n".join(lines)
