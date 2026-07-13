"""The one new read selector the dashboard adds: tickets grouped by FSM state (#3162).

The board axis is ``Ticket.state`` — the lifecycle position — never the task/queue
axis the pre-#541 dashboard centred on (tasks appear only as card badges). Every
other panel reads through the existing ``teatree.core.selectors`` /
``operational_health`` / ``loops.live`` readers unchanged; this module adds
``build_kanban_columns`` beside them and nothing else queries ticket state for the
board. The column set is a module constant a conformance test pins against the FSM
enum, so a future ``Ticket.State`` value cannot silently drop off the board.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from django.db.models import Max
from django.utils import timezone

from teatree.core.models.pull_request import PullRequest
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.selectors import _humanize_duration

State = Ticket.State

# The four visual groupings (lifecycle order). The union of every grouped state
# plus the toggle-hidden IGNORED equals ``Ticket.State`` exactly — pinned by
# ``test_kanban_conformance``. A card keeps its own state sub-column within a
# group; the grouping is purely visual.
COLUMN_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Backlog", (State.NOT_STARTED, State.SCOPED)),
    ("Building", (State.STARTED, State.PLANNED, State.CODED, State.TESTED)),
    ("Reviewing", (State.REVIEWED, State.SHIPPED, State.IN_REVIEW)),
    ("Landed", (State.MERGED, State.RETROSPECTED, State.DELIVERED)),
)
# Hidden by default behind the board's ``ignored`` toggle (abandoned tickets).
HIDDEN_STATES: tuple[str, ...] = (State.IGNORED,)

# Every column the board renders, in lifecycle order (excludes IGNORED).
BOARD_COLUMNS: tuple[str, ...] = tuple(state for _group, states in COLUMN_GROUPS for state in states)

# state -> group slug (backlog/building/reviewing/landed/ignored), the ONE mapping
# behind the FSM-rail hue language: column tops, the rail, and the drawer's
# transition-history + current-state chip all colour by it, so board, diagram and
# history speak one hue set. Derived from COLUMN_GROUPS so it cannot drift.
_GROUP_SLUG_BY_STATE: dict[str, str] = {state: name.lower() for name, states in COLUMN_GROUPS for state in states}


def group_slug(state: str) -> str:
    """The FSM-rail group slug a state belongs to (IGNORED / unknown -> ``ignored``)."""
    return _GROUP_SLUG_BY_STATE.get(str(state), "ignored")


@dataclass(frozen=True, slots=True)
class PrChip:
    url: str
    repo: str
    iid: str
    state: str


@dataclass(frozen=True, slots=True)
class KanbanCard:
    ticket_id: int
    number: str
    short_description: str
    issue_url: str
    overlay: str
    role: str
    kind: str
    expedited: bool
    remote_missing: bool
    active: bool
    active_phase: str
    claimed_by: str
    last_error: str
    dwell: str
    pr_chips: tuple[PrChip, ...] = ()


@dataclass(frozen=True, slots=True)
class KanbanColumn:
    state: str
    label: str
    cards: tuple[KanbanCard, ...]

    @property
    def count(self) -> int:
        return len(self.cards)


@dataclass(frozen=True, slots=True)
class KanbanGroup:
    name: str
    columns: tuple[KanbanColumn, ...]


@dataclass(frozen=True, slots=True)
class KanbanBoard:
    groups: tuple[KanbanGroup, ...]
    include_ignored: bool
    ignored: KanbanColumn | None = None

    @property
    def total(self) -> int:
        shown = sum(column.count for group in self.groups for column in group.columns)
        return shown + (self.ignored.count if self.ignored else 0)


@dataclass(frozen=True, slots=True)
class BoardFilters:
    overlay: str = ""
    role: str = ""
    kind: str = ""
    text: str = ""
    include_ignored: bool = False


@dataclass(frozen=True, slots=True)
class _CardContext:
    """The bulk-fetched per-ticket signals a card renders, keyed by ticket id."""

    active_ticket_ids: set[int] = field(default_factory=set)
    active_phase: dict[int, str] = field(default_factory=dict)
    claimed_by: dict[int, str] = field(default_factory=dict)
    last_error: dict[int, str] = field(default_factory=dict)
    latest_transition_at: dict[int, datetime] = field(default_factory=dict)
    pr_chips: dict[int, tuple[PrChip, ...]] = field(default_factory=dict)


def build_kanban_columns(filters: BoardFilters | None = None) -> KanbanBoard:
    """Group the ticket set into FSM-state columns with per-card badge signals.

    Read-only: it issues ORM reads only and never touches the FSM. Every
    per-ticket badge (active-work dot, active phase / claimed-by, last task
    error, dwell-in-column, PR chips) is bulk-fetched once for the whole board
    rather than per card, so the render is a small fixed number of queries.
    """
    active = filters or BoardFilters()
    tickets = _filtered_tickets(active)
    ticket_ids = [t.pk for t in tickets]
    context = _card_context(ticket_ids)

    by_state: dict[str, list[KanbanCard]] = {state: [] for state in (*BOARD_COLUMNS, *HIDDEN_STATES)}
    for ticket in tickets:
        by_state.setdefault(str(ticket.state), []).append(_card(ticket, context))

    groups = tuple(
        KanbanGroup(
            name=name,
            columns=tuple(
                KanbanColumn(state=state, label=_label(state), cards=tuple(by_state[state])) for state in states
            ),
        )
        for name, states in COLUMN_GROUPS
    )
    ignored = (
        KanbanColumn(state=State.IGNORED, label=_label(State.IGNORED), cards=tuple(by_state[State.IGNORED]))
        if active.include_ignored
        else None
    )
    return KanbanBoard(groups=groups, include_ignored=active.include_ignored, ignored=ignored)


def _filtered_tickets(filters: BoardFilters) -> list[Ticket]:
    columns = (*BOARD_COLUMNS, *HIDDEN_STATES) if filters.include_ignored else BOARD_COLUMNS
    qs = Ticket.objects.filter(state__in=columns)
    if filters.overlay:
        qs = qs.filter(overlay=filters.overlay)
    if filters.role:
        qs = qs.filter(role=filters.role)
    if filters.kind:
        qs = qs.filter(kind=filters.kind)
    if filters.text:
        qs = qs.filter(short_description__icontains=filters.text) | qs.filter(issue_url__icontains=filters.text)
    return list(qs.order_by("-id"))


def _card_context(ticket_ids: list[int]) -> _CardContext:
    if not ticket_ids:
        return _CardContext()
    return _CardContext(
        active_ticket_ids=_active_ticket_ids(ticket_ids),
        active_phase=_active_phase(ticket_ids),
        claimed_by=_claimed_by(ticket_ids),
        last_error=_last_error_by_ticket(ticket_ids),
        latest_transition_at=_latest_transition_at(ticket_ids),
        pr_chips=_pr_chips_by_ticket(ticket_ids),
    )


def _active_ticket_ids(ticket_ids: list[int]) -> set[int]:
    # Query Session / Task directly, not `Ticket.filter(sessions__ended_at__isnull=True)`:
    # a reverse-relation `isnull=True` filter promotes to a LEFT JOIN, so a ticket
    # with NO sessions would spuriously match (the null-row's `ended_at` IS NULL).
    with_open_session = set(
        Session.objects.filter(ticket_id__in=ticket_ids, ended_at__isnull=True).values_list("ticket_id", flat=True),
    )
    with_active_task = set(
        Task.objects.filter(ticket_id__in=ticket_ids, status__in=Task.Status.active()).values_list(
            "ticket_id",
            flat=True,
        ),
    )
    return with_open_session | with_active_task


def _active_phase(ticket_ids: list[int]) -> dict[int, str]:
    rows = (
        Task.objects.filter(ticket_id__in=ticket_ids, status__in=Task.Status.active())
        .order_by("ticket_id", "-pk")
        .values_list("ticket_id", "phase")
    )
    phases: dict[int, str] = {}
    for ticket_id, phase in rows:
        phases.setdefault(ticket_id, phase)
    return phases


def _claimed_by(ticket_ids: list[int]) -> dict[int, str]:
    rows = (
        Task.objects.filter(ticket_id__in=ticket_ids, status=Task.Status.CLAIMED)
        .exclude(claimed_by="")
        .order_by("ticket_id", "-pk")
        .values_list("ticket_id", "claimed_by")
    )
    claimed: dict[int, str] = {}
    for ticket_id, actor in rows:
        claimed.setdefault(ticket_id, actor)
    return claimed


def _last_error_by_ticket(ticket_ids: list[int]) -> dict[int, str]:
    latest_pks = (
        TaskAttempt.objects.filter(task__ticket_id__in=ticket_ids, error__gt="")
        .values("task__ticket_id")
        .annotate(latest_pk=Max("pk"))
        .values_list("latest_pk", flat=True)
    )
    attempts = TaskAttempt.objects.filter(pk__in=list(latest_pks)).select_related("task")
    return {attempt.task.ticket_id: attempt.error for attempt in attempts}


def _latest_transition_at(ticket_ids: list[int]) -> dict[int, datetime]:
    rows = (
        TicketTransition.objects.filter(ticket_id__in=ticket_ids)
        .values("ticket_id")
        .annotate(latest=Max("created_at"))
        .values_list("ticket_id", "latest")
    )
    return dict(rows)


def _pr_chips_by_ticket(ticket_ids: list[int]) -> dict[int, tuple[PrChip, ...]]:
    chips: dict[int, list[PrChip]] = {}
    for pr in PullRequest.objects.filter(ticket_id__in=ticket_ids).order_by("ticket_id", "pk"):
        chips.setdefault(pr.ticket_id, []).append(
            PrChip(url=pr.url, repo=pr.repo, iid=pr.iid, state=str(pr.state)),
        )
    return {ticket_id: tuple(prs) for ticket_id, prs in chips.items()}


def _card(ticket: Ticket, context: _CardContext) -> KanbanCard:
    return KanbanCard(
        ticket_id=ticket.pk,
        number=ticket.ticket_number,
        short_description=ticket.short_description,
        issue_url=ticket.issue_url,
        overlay=ticket.overlay,
        role=ticket.role,
        kind=ticket.kind,
        expedited=ticket.expedited,
        remote_missing=ticket.remote_missing,
        active=ticket.pk in context.active_ticket_ids,
        active_phase=context.active_phase.get(ticket.pk, ""),
        claimed_by=context.claimed_by.get(ticket.pk, ""),
        last_error=context.last_error.get(ticket.pk, ""),
        dwell=_dwell(context.latest_transition_at.get(ticket.pk)),
        pr_chips=context.pr_chips.get(ticket.pk, ()),
    )


def _dwell(latest_transition_at: datetime | None) -> str:
    if latest_transition_at is None:
        return ""
    return _humanize_duration((timezone.now() - latest_transition_at).total_seconds())


def _label(state: str) -> str:
    return State(state).label


def all_column_states() -> Iterable[str]:
    """Every state the board can render — the four groups plus the hidden set.

    The conformance test asserts this equals ``Ticket.State`` so no FSM state
    can silently vanish from the board.
    """
    return (*BOARD_COLUMNS, *HIDDEN_STATES)
