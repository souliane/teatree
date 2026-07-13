"""The ticket-detail drawer read model + the legal-transition set (#3162).

The actions menu offers ONLY the transitions django-fsm reports as legal from the
ticket's current state (``get_available_state_transitions`` — conditions honoured),
so an illegal move is never even rendered; the POST re-checks the same set before
calling the guarded model method. History and the lifecycle diagram reuse the
recorded ``TicketTransition`` rows and ``build_ticket_lifecycle_mermaid``.
"""

from dataclasses import dataclass
from datetime import datetime

from teatree.core.models.pull_request import PullRequest
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.selectors import build_ticket_lifecycle_mermaid
from teatree.dash.selectors import PrChip, group_slug


@dataclass(frozen=True, slots=True)
class TransitionRow:
    from_state: str
    to_state: str
    triggered_by: str
    created_at: datetime
    session_label: str
    from_group: str
    to_group: str


@dataclass(frozen=True, slots=True)
class TaskRow:
    task_id: int
    phase: str
    status: str
    claimed_by: str
    execution_target: str


@dataclass(frozen=True, slots=True)
class SessionRow:
    session_id: int
    agent_id: str
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class TicketDetail:
    ticket_id: int
    number: str
    state: str
    state_label: str
    state_group: str
    overlay: str
    role: str
    kind: str
    issue_url: str
    short_description: str
    expedited: bool
    remote_missing: bool
    transitions: tuple[TransitionRow, ...]
    mermaid: str
    available_transitions: tuple[str, ...]
    tasks: tuple[TaskRow, ...]
    sessions: tuple[SessionRow, ...]
    pr_chips: tuple[PrChip, ...]


def legal_transition_names(ticket: Ticket) -> tuple[str, ...]:
    """The transition names legal from the ticket's current state, django-fsm-introspected.

    Sorted and de-duplicated — the single source the drawer menu renders and the
    POST validates against, so the menu can never offer a move the model refuses.

    Evaluated defensively rather than via ``get_available_state_transitions()``:
    some guard conditions are hard gates that RAISE (e.g. the plan-artifact gate
    raises when no plan exists) instead of returning ``False``, which would abort
    the whole introspection. A raising condition simply means "not legal now".
    """
    current = str(ticket.state)
    names: set[str] = set()
    for transition in ticket.get_all_state_transitions():  # ty: ignore[unresolved-attribute]  # django-fsm dynamic method
        if str(transition.source) != current:
            continue
        try:
            if all(condition(ticket) for condition in transition.conditions):
                names.add(transition.name)
        except Exception:  # noqa: BLE001, S112 — a hard-gate condition that raises means "not legal now"
            continue
    return tuple(sorted(names))


def build_ticket_detail(ticket_id: int) -> TicketDetail:
    ticket = Ticket.objects.get(pk=ticket_id)
    return TicketDetail(
        ticket_id=ticket.pk,
        number=ticket.ticket_number,
        state=str(ticket.state),
        state_label=Ticket.State(ticket.state).label,
        state_group=group_slug(ticket.state),
        overlay=ticket.overlay,
        role=ticket.role,
        kind=ticket.kind,
        issue_url=ticket.issue_url,
        short_description=ticket.short_description,
        expedited=ticket.expedited,
        remote_missing=ticket.remote_missing,
        transitions=_transitions(ticket_id),
        mermaid=build_ticket_lifecycle_mermaid(ticket_id),
        available_transitions=legal_transition_names(ticket),
        tasks=_tasks(ticket),
        sessions=_sessions(ticket),
        pr_chips=_pr_chips(ticket_id),
    )


def _transitions(ticket_id: int) -> tuple[TransitionRow, ...]:
    rows = TicketTransition.objects.filter(ticket_id=ticket_id).select_related("session").order_by("created_at")
    return tuple(
        TransitionRow(
            from_state=row.from_state,
            to_state=row.to_state,
            triggered_by=row.triggered_by,
            created_at=row.created_at,
            session_label=str(row.session.agent_id) if row.session_id else "",
            from_group=group_slug(row.from_state),
            to_group=group_slug(row.to_state),
        )
        for row in rows
    )


def _tasks(ticket: Ticket) -> tuple[TaskRow, ...]:
    return tuple(
        TaskRow(
            task_id=task.pk,
            phase=task.phase,
            status=task.get_status_display(),
            claimed_by=task.claimed_by,
            execution_target=task.execution_target,
        )
        for task in ticket.tasks.order_by("-pk")  # ty: ignore[unresolved-attribute]  # Django reverse FK
    )


def _sessions(ticket: Ticket) -> tuple[SessionRow, ...]:
    return tuple(
        SessionRow(
            session_id=session.pk,
            agent_id=session.agent_id,
            started_at=session.started_at,
            ended_at=session.ended_at,
        )
        for session in ticket.sessions.order_by("-started_at")  # ty: ignore[unresolved-attribute]  # Django reverse FK
    )


def _pr_chips(ticket_id: int) -> tuple[PrChip, ...]:
    return tuple(
        PrChip(url=pr.url, repo=pr.repo, iid=pr.iid, state=str(pr.state))
        for pr in PullRequest.objects.filter(ticket_id=ticket_id).order_by("pk")
    )
