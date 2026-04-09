from teatree.core.models import Task, Ticket, TicketTransition

from ._types import (
    TaskAttemptDetail,
    TaskDetail,
    TaskGraphNode,
    TaskRelatedRow,
)


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
            execution_target=p.get_execution_target_display(),
            execution_reason=p.execution_reason[:120],
        )

    children = [
        TaskRelatedRow(
            task_id=c.pk,
            phase=c.phase,
            status=c.get_status_display(),
            execution_target=c.get_execution_target_display(),
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
            execution_target=a.get_execution_target_display(),
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
        execution_target=task.get_execution_target_display(),
        execution_reason=task.execution_reason,
        claimed_by=task.claimed_by,
        session_agent_id=task.session.agent_id if task.session_id else "",
        parent=parent,
        children=children,
        attempts=attempts,
    )


def build_task_graph(ticket_id: int) -> list[TaskGraphNode]:
    """Build a tree of tasks for a ticket, rooted at tasks with no parent."""
    tasks = list(Task.objects.filter(ticket_id=ticket_id).select_related("parent_task").order_by("pk"))
    children_map: dict[int | None, list[Task]] = {}
    for task in tasks:
        children_map.setdefault(task.parent_task_id, []).append(task)

    def _build(parent_id: int | None, depth: int) -> list[TaskGraphNode]:
        return [
            TaskGraphNode(
                task_id=task.pk,
                phase=task.phase,
                status=task.get_status_display(),  # ty: ignore[unresolved-attribute]
                execution_target=task.get_execution_target_display(),  # ty: ignore[unresolved-attribute]
                execution_reason=task.execution_reason[:120],
                depth=depth,
                children=_build(task.pk, depth + 1),
            )
            for task in children_map.get(parent_id, [])
        ]

    return _build(None, 0)


def build_ticket_lifecycle_mermaid(ticket_id: int) -> str:
    """Build a Mermaid stateDiagram-v2 from recorded TicketTransition rows."""
    ticket = Ticket.objects.get(pk=ticket_id)
    transitions = TicketTransition.objects.filter(ticket_id=ticket_id).select_related("session")

    lines = ["stateDiagram-v2", f"    [*] --> {ticket.State.NOT_STARTED}"]

    for t in transitions:
        label = f"{t.triggered_by}()"
        if t.session_id:
            label += f" S{t.session_id}"
        lines.append(f"    {t.from_state} --> {t.to_state}: {label}")

    # Highlight current state with a note.
    lines.append(f"    note right of {ticket.state}: current")

    return "\n".join(lines)
