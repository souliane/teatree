"""Read-only Remaining group for the checking report."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from django.db.models import Q

from teatree.core._checking_gather import _merge_safe_prs, _recent_failed_tasks, ticket_url
from teatree.core.admission_priority import ADMISSION_ORDER, admission_priority_annotations
from teatree.core.agent_admission import headless_lane_widths
from teatree.core.checking_types import DEFAULT_CAP, CheckGroup, CheckGroupDict, CheckItem, CheckItemDict
from teatree.core.cycle_time.eta import format_minutes, queue_waves, remaining_given_elapsed
from teatree.core.cycle_time.step_history import StepEstimate, StepHistory
from teatree.core.lifecycle_pipeline import PIPELINE, TicketSnapshot, resolve_plan
from teatree.core.managers import ADMITTED_INFLIGHT_WINDOW
from teatree.core.modelkit.phases import REVIEW_PHASES, CheapClass, normalize_phase
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket

STEP_PHASE = {
    "rework": "coding",
    "factory review": "reviewing",
    "independent review": "reviewing",
}
POST_PR = ("independent review", "merge", "deploy")
PHASE_LABEL = {"reviewing": "factory review", "coding": "coding"}
NO_WORK_BASIS = "no current work in scope"


class RemainingItemDict(CheckItemDict):
    kind: str
    steps: list[str]
    eta: dict


class RemainingGroupDict(CheckGroupDict):
    eta: dict
    queue: dict
    durations: list[dict]
    holds: dict


@dataclass(frozen=True, slots=True)
class RemainingItem(CheckItem):
    kind: str = ""
    steps: tuple[str, ...] = ()
    eta: dict = field(default_factory=dict)
    duration_phases: tuple[str, ...] = ()
    ticket_id: int | None = None
    hold: str = ""

    def to_dict(self) -> RemainingItemDict:
        return cast(
            "RemainingItemDict",
            {**super().to_dict(), "kind": self.kind, "steps": list(self.steps), "eta": self.eta},
        )


@dataclass(frozen=True, slots=True)
class RemainingGroup(CheckGroup):
    eta: dict = field(default_factory=dict)
    queue: dict = field(default_factory=dict)
    durations: list[dict] = field(default_factory=list)
    holds: dict = field(default_factory=dict)

    def to_dict(self) -> RemainingGroupDict:
        return RemainingGroupDict(
            title=self.title,
            items=[item.to_dict() for item in self.items],
            total=self.total,
            eta=self.eta,
            queue=self.queue,
            durations=self.durations,
            holds=self.holds,
        )


def empty_remaining(*, dormant: int = 0) -> RemainingGroup:
    return RemainingGroup(
        title="Remaining",
        eta={
            "median_minutes": None,
            "p80_minutes": None,
            "known": 0,
            "unknown": 0,
            "basis": NO_WORK_BASIS,
        },
        queue={"claimed": {}, "pending": {}, "oldest_pending_minutes": None, "oldest_pending_phase": None, "seats": {}},
        holds={"halted": 0, "blocked": 0, "owner": 0, "dormant": dormant},
    )


def _remaining_pr_steps(phase: str, *, merge_safe: bool) -> tuple[str, ...]:
    if phase in {"coding", "debugging"}:
        before = ("rework", "testing", "factory review")
    elif phase == "testing":
        before = ("testing", "factory review")
    elif phase in REVIEW_PHASES:
        before = ("factory review",)
    elif phase == "shipping":
        return ("shipping", "merge", "deploy") if merge_safe else ("shipping", *POST_PR)
    elif merge_safe:
        return ("merge", "deploy")
    else:
        before = ()
    return (*before, *POST_PR)


def remaining_steps(state: str, active_phase: str, *, has_pr: bool, merge_safe: bool) -> tuple[str, ...]:
    phase = normalize_phase(active_phase)
    if state in {Ticket.State.MERGED, Ticket.State.RETRO_RECORDED}:
        return ("deploy",)
    if has_pr:
        return _remaining_pr_steps(phase, merge_safe=merge_safe)
    if phase in {"debugging", "coding"}:
        phase_steps = ("coding", "testing", "factory review", "shipping")
    else:
        snapshot = TicketSnapshot(exists=True, state=state, provisioned=True, ignored=False)
        phase_steps = tuple(
            "shipping" if step.name == "ship" else PHASE_LABEL.get(step.needs, step.needs)
            for step, status in resolve_plan(snapshot)
            if step in PIPELINE and (step.needs or step.name == "ship") and status.value != "done"
        )
        label = PHASE_LABEL.get(phase, phase)
        if label in phase_steps:
            phase_steps = phase_steps[phase_steps.index(label) :]
    return (*phase_steps, *POST_PR)


def _lane(phase: str) -> str:
    cheap = CheapClass.of(phase)
    return cheap.value if cheap is not None else "expensive"


def _running_measured(estimate: StepEstimate, elapsed: float) -> tuple[tuple[float, float] | None, str]:
    remaining = remaining_given_elapsed(estimate.samples, elapsed)
    if remaining is None:
        return None, f"running longer than all {estimate.n} {estimate.window} samples"
    return remaining, ""


def _duration_phase(step: str, task: Task | None, index: int) -> str:
    basis = STEP_PHASE.get(step, step)
    if index or task is None:
        return basis
    phase = normalize_phase(task.phase)
    if step in {"rework", "coding"} and phase in {"coding", "debugging"}:
        return phase
    if step == "factory review" and phase in REVIEW_PHASES:
        return phase
    return phase if step == PHASE_LABEL.get(phase, phase) else basis


@dataclass(frozen=True, slots=True)
class QueueState:
    now: datetime
    pending: list[Task]
    claimed: Counter[str]
    pending_counts: Counter[str]
    occupied: Counter[str]
    seats: dict[str, int]
    oldest: datetime | None
    oldest_phase: str | None

    def has_seat(self, task: Task | None) -> bool:
        return bool(
            task
            and task.status == Task.Status.PENDING
            and task.admitted_at
            and task.admitted_at > self.now - ADMITTED_INFLIGHT_WINDOW
            and (task.not_before is None or task.not_before <= self.now)
        )

    def ahead(self, task: Task | None, phase: str) -> int:
        lane = _lane(phase)
        lane_tasks = [row for row in self.pending if _lane(row.phase) == lane]
        if task is None:
            return len(lane_tasks)
        if self.has_seat(task):
            return 0
        return next((index for index, row in enumerate(lane_tasks) if row.pk == task.pk), len(lane_tasks))

    @classmethod
    def from_tasks(cls, tasks: list[Task], waiting: list[Task], now: datetime) -> "QueueState":
        widths = headless_lane_widths()
        pending = waiting
        oldest = min(
            (task for task in pending if task.created_at), key=lambda task: (task.created_at, task.pk), default=None
        )
        return cls(
            now=now,
            pending=pending,
            claimed=Counter(normalize_phase(task.phase) for task in tasks if task.status == Task.Status.CLAIMED),
            pending_counts=Counter(normalize_phase(task.phase) for task in tasks if task.status == Task.Status.PENDING),
            occupied=Counter(
                {
                    "expensive": Task.objects.expensive_lane_occupancy(),
                    "review": Task.objects.cheap_class_occupancy(CheapClass.REVIEW),
                    "retiring": Task.objects.cheap_class_occupancy(CheapClass.RETIRING),
                }
            ),
            seats={"expensive": widths.expensive, "review": widths.review, "retiring": widths.retiring},
            oldest=oldest.created_at if oldest else None,
            oldest_phase=normalize_phase(oldest.phase) if oldest else None,
        )

    def to_dict(self) -> dict:
        return {
            "claimed": dict(self.claimed),
            "pending": dict(self.pending_counts),
            "oldest_pending_minutes": round((self.now - self.oldest).total_seconds() / 60, 1) if self.oldest else None,
            "oldest_pending_phase": self.oldest_phase,
            "seats": self.seats,
        }


def _eta_for_item(
    *,
    steps: tuple[str, ...],
    duration_phases: tuple[str, ...],
    task: Task | None,
    history: StepHistory,
    queue: QueueState,
) -> tuple[dict, str]:
    median = p80 = 0.0
    defaults: set[str] = set()
    for index, (step, phase) in enumerate(zip(steps, duration_phases, strict=True)):
        estimate = history.estimate(phase)
        if estimate is None:
            return {
                "median_minutes": None,
                "p80_minutes": None,
                "basis": "no default: no completed-task history",
            }, "ETA unknown (no default: no completed-task history)"
        if (
            index == 0
            and step not in {"merge", "deploy"}
            and task is not None
            and task.status == Task.Status.CLAIMED
            and task.admitted_at
        ):
            elapsed = max(0.0, (queue.now - task.admitted_at).total_seconds() / 60)
            if estimate.basis == "measured":
                remaining, reason = _running_measured(estimate, elapsed)
                if remaining is None:
                    return {"median_minutes": None, "p80_minutes": None, "basis": reason}, f"ETA unknown ({reason})"
                median += remaining[0]
                p80 += remaining[1]
            else:
                remaining_default = history.remaining_default(elapsed)
                if remaining_default is None:
                    reason = "running longer than all measured fallback samples"
                    return {"median_minutes": None, "p80_minutes": None, "basis": reason}, f"ETA unknown ({reason})"
                median += remaining_default[0]
                p80 += remaining_default[1]
                defaults.add(f"{phase}: {remaining_default[2]}")
        else:
            if estimate.basis != "measured":
                defaults.add(f"{phase}: {estimate.basis}")
            if index == 0 and step not in {"merge", "deploy"} and not queue.has_seat(task):
                queue_phase = task.phase if task is not None else phase
                lane = _lane(queue_phase)
                ahead = queue.ahead(task, queue_phase)
                waves = queue_waves(ahead=ahead, occupied=queue.occupied[lane], seats=queue.seats[lane])
                if waves is None:
                    reason = f"no seats for {lane} lane"
                    return {"median_minutes": None, "p80_minutes": None, "basis": reason}, f"ETA unknown ({reason})"
                median += waves * estimate.median_minutes
                p80 += waves * estimate.p80_minutes
            median += estimate.median_minutes
            p80 += estimate.p80_minutes
    result = {
        "median_minutes": round(median, 1),
        "p80_minutes": round(p80, 1),
        "basis": "sum of step medians and p80s; queue wait by seat waves"
        + (f"; {'; '.join(sorted(defaults))}" if defaults else ""),
    }
    default_note = f" ({'; '.join(sorted(defaults))})" if defaults else ""
    return result, f"ETA {format_minutes(median)}\u2013{format_minutes(p80)}{default_note}"


@dataclass(frozen=True, slots=True)
class RemainingSnapshot:
    now: datetime
    tag_overlays: bool
    prs: list[PullRequest]
    active: list[Task]
    owner_questions: list[DeferredQuestion]
    owner_task_ids: set[int]
    recent_failed: dict[int, str]
    open_author: list[Ticket]
    dormant: int
    verdicts: set[tuple[str, int]]
    queue: QueueState

    @classmethod
    def load(cls, *, now: datetime, overlay_names: list[str] | None, tag_overlays: bool) -> "RemainingSnapshot":
        task_scope = Q(ticket__overlay__in=overlay_names) if overlay_names is not None else Q()
        ticket_scope = Q(overlay__in=overlay_names) if overlay_names is not None else Q()
        question_scope = Q(parked_task__ticket__overlay__in=overlay_names) if overlay_names is not None else Q()
        prs = list(
            PullRequest.objects.live()
            .filter(task_scope)
            .exclude(ticket__state__in=[Ticket.State.DELIVERED, Ticket.State.REVIEW_DELIVERED, Ticket.State.IGNORED])
            .select_related("ticket")
            .order_by("-pk")
        )
        active = list(
            Task.objects.filter(status__in=Task.Status.active())
            .filter(task_scope)
            .select_related("ticket")
            .order_by("created_at", "pk")
        )
        owner_questions = list(
            DeferredQuestion.owner_pending()
            .filter(
                question_scope,
                parked_task__ticket__role=Ticket.Role.AUTHOR,
                parked_task__status=Task.Status.PENDING,
            )
            .select_related("parked_task")
        )
        recent_failed = _recent_failed_tasks(
            now, Q(task__ticket__overlay__in=overlay_names) if overlay_names is not None else Q()
        )
        relevant_ids = (
            {pr.ticket_id for pr in prs}
            | {task.ticket_id for task in active}
            | {question.parked_task.ticket_id for question in owner_questions if question.parked_task is not None}
            | recent_failed.keys()
        )
        open_tickets = Ticket.objects.filter(ticket_scope, role=Ticket.Role.AUTHOR).exclude(
            state__in=[Ticket.State.DELIVERED, Ticket.State.REVIEW_DELIVERED, Ticket.State.IGNORED]
        )
        open_author = list(open_tickets.filter(pk__in=relevant_ids).order_by("-pk"))
        dormant = open_tickets.exclude(pk__in=relevant_ids).exclude(state__in=Ticket.finished_states()).count()
        return cls(
            now=now,
            tag_overlays=tag_overlays,
            prs=prs,
            active=active,
            owner_questions=owner_questions,
            owner_task_ids={row.parked_task_id for row in owner_questions if row.parked_task_id is not None},
            recent_failed=recent_failed,
            open_author=open_author,
            dormant=dormant,
            verdicts=_merge_safe_prs(prs),
            queue=QueueState.from_tasks(
                list(Task.objects.filter(status__in=Task.Status.active())),
                list(
                    Task.objects.waiting_for_admission()
                    .annotate(**admission_priority_annotations())
                    .order_by(*ADMISSION_ORDER)
                ),
                now,
            ),
        )


def _work_item(
    snapshot: RemainingSnapshot,
    history: StepHistory,
    ticket: Ticket,
    pr: PullRequest | None,
    tasks: list[Task],
) -> RemainingItem:
    task = next((row for row in tasks if row.status == Task.Status.CLAIMED), tasks[0] if tasks else None)
    hold, status = _hold_for_item(snapshot, ticket, tasks)
    merge_safe = bool(pr and pr.iid.isdigit() and (pr.repo.casefold(), int(pr.iid)) in snapshot.verdicts)
    steps = remaining_steps(ticket.state, task.phase if task else "", has_pr=pr is not None, merge_safe=merge_safe)
    duration_phases = tuple(_duration_phase(step, task, index) for index, step in enumerate(steps))
    if hold:
        eta = {"median_minutes": None, "p80_minutes": None, "basis": hold}
        eta_text = f"ETA unknown ({hold})"
    else:
        eta, eta_text = _eta_for_item(
            steps=steps, duration_phases=duration_phases, task=task, history=history, queue=snapshot.queue
        )
    overlay_tag = f" [{ticket.overlay}]" if snapshot.tag_overlays else ""
    if task and task.status == Task.Status.CLAIMED:
        current = f"{steps[0]} (running)"
    elif task:
        ahead = snapshot.queue.ahead(task, task.phase)
        current = f"{steps[0]} (queued, {ahead} ahead)"
    else:
        current = steps[0]
    later = f"; then {', '.join(steps[1:])}" if len(steps) > 1 else ""
    detail = f"{status}now: {current}{later} · {eta_text}{overlay_tag}"
    return RemainingItem(
        label=f"{pr.repo}#{pr.iid}" if pr else f"#{ticket.ticket_number}",
        url=pr.url if pr else ticket_url(ticket),
        title=ticket.short_description or "work in progress",
        detail=detail,
        kind="pr" if pr else "ticket",
        steps=steps,
        eta=eta,
        duration_phases=duration_phases,
        ticket_id=ticket.pk,
        hold=hold,
    )


def _hold_for_item(snapshot: RemainingSnapshot, ticket: Ticket, tasks: list[Task]) -> tuple[str, str]:
    if tasks and all(row.pk in snapshot.owner_task_ids for row in tasks):
        return "waiting on you", "waiting on you · "
    if tasks and all(row.not_before and row.not_before > snapshot.now for row in tasks):
        until = min(row.not_before for row in tasks if row.not_before is not None)
        return "blocked", f"blocked (parked until {until:%H:%M} UTC) · "
    if not tasks and ticket.pk in snapshot.recent_failed:
        kind = snapshot.recent_failed[ticket.pk]
        return "halted", f"halted ({dict(FailureKind.choices).get(kind, kind).lower()}) · "
    return "", ""


def _duration_dict(history: StepHistory, step: str) -> dict:
    estimate = history.estimate(step)
    if estimate is not None:
        return estimate.to_dict()
    return {
        "step": step,
        "median_minutes": None,
        "p80_minutes": None,
        "n": 0,
        "window": "7d",
        "source": "no completed samples",
        "basis": "no default: no completed-task history",
    }


def _eta_totals(items: list[RemainingItem]) -> tuple[dict, Counter[str]]:
    by_ticket: dict[int | None, list[RemainingItem]] = defaultdict(list)
    for item in items:
        by_ticket[item.ticket_id].append(item)
    known_groups = [
        group for group in by_ticket.values() if all(item.eta["median_minutes"] is not None for item in group)
    ]
    known = [item.eta for group in known_groups for item in group]
    unknown = sum(
        any(item.eta["median_minutes"] is None and not item.hold for item in group) for group in by_ticket.values()
    )
    held = Counter(group[0].hold for group in by_ticket.values() if group[0].hold)
    unknown_bases = {item.eta["basis"] for item in items if item.eta["median_minutes"] is None and not item.hold}
    if unknown_bases:
        basis = "; ".join(sorted(unknown_bases))
    elif known:
        basis = "maximum known item range; held items excluded"
    else:
        basis = "all current work is held" if items else NO_WORK_BASIS
    median_total = max((float(row["median_minutes"]) for row in known), default=None) if not unknown_bases else None
    p80_total = max((float(row["p80_minutes"]) for row in known), default=None) if not unknown_bases else None
    eta_total = {
        "median_minutes": median_total,
        "p80_minutes": p80_total,
        "known": len(known_groups),
        "unknown": unknown,
        "basis": basis,
    }
    return eta_total, held


def _summarize(snapshot: RemainingSnapshot, history: StepHistory, items: list[RemainingItem]) -> RemainingGroup:
    eta_total, held = _eta_totals(items)
    median_total = eta_total["median_minutes"]
    p80_total = eta_total["p80_minutes"]
    basis = eta_total["basis"]
    unknown = eta_total["unknown"]
    total_text = (
        f"ETA {format_minutes(median_total)}\u2013{format_minutes(p80_total)}"
        if median_total is not None and p80_total is not None
        else f"ETA unknown ({basis})"
    )
    owner_count = held["waiting on you"]
    durations = [
        _duration_dict(history, step) for step in sorted({phase for item in items for phase in item.duration_phases})
    ]
    legend = "; ".join(
        f"{row['step']} {row['median_minutes']}\u2013{row['p80_minutes']}m n={row['n']} {row['window']} "
        f"{row['source']} ({row['basis']})"
        for row in durations
        if row["n"]
    )
    summary = RemainingItem(
        label="Total",
        url="",
        kind="summary",
        detail=(
            f"{total_text} · {len(items)} items, {unknown} unknown, {held['halted']} halted, "
            f"{held['blocked']} blocked, {owner_count} waiting on you, {snapshot.dormant} dormant"
            + (f" · basis: {legend}" if legend else "")
        ),
    )
    queue = snapshot.queue.to_dict()
    queue_item = RemainingItem(
        label="Queue",
        url="",
        kind="queue",
        detail=(
            f"claimed {_format_counts(queue['claimed'])}; pending {_format_counts(queue['pending'])}; oldest "
            f"{format_minutes(queue['oldest_pending_minutes']) if snapshot.queue.oldest else 'none'} "
            f"{queue['oldest_pending_phase'] or ''}; "
            f"seats {_format_counts(queue['seats'])}"
        ),
    )
    return RemainingGroup(
        title="Remaining",
        items=[summary, queue_item, *items[: DEFAULT_CAP - 2]],
        total=len(items) + 2,
        eta=eta_total,
        queue=queue,
        durations=durations,
        holds={"halted": held["halted"], "blocked": held["blocked"], "owner": owner_count, "dormant": snapshot.dormant},
    )


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in sorted(counts.items())) or "none"


def gather_remaining(*, now: datetime, overlay_names: list[str] | None, tag_overlays: bool) -> RemainingGroup:
    snapshot = RemainingSnapshot.load(now=now, overlay_names=overlay_names, tag_overlays=tag_overlays)
    active_by_ticket: dict[int, list[Task]] = defaultdict(list)
    for task in snapshot.active:
        active_by_ticket[task.ticket_id].append(task)
    owner_ticket_ids = {
        parked_task.ticket_id
        for question in snapshot.owner_questions
        if (parked_task := question.parked_task) is not None
    }
    pr_ticket_ids = {pr.ticket_id for pr in snapshot.prs}
    relevant = pr_ticket_ids | active_by_ticket.keys() | owner_ticket_ids | snapshot.recent_failed.keys()
    work: list[tuple[Ticket, PullRequest | None]] = [
        (pr.ticket, pr) for pr in snapshot.prs if pr.ticket.role == Ticket.Role.AUTHOR
    ]
    work.extend(
        (ticket, None) for ticket in snapshot.open_author if ticket.pk in relevant and ticket.pk not in pr_ticket_ids
    )
    if not work and not snapshot.active and not snapshot.owner_questions:
        return empty_remaining(dormant=snapshot.dormant)
    history = StepHistory.measure(now) if work else StepHistory(now=now, spans={})
    items = sorted(
        (_work_item(snapshot, history, ticket, pr, active_by_ticket.get(ticket.pk, [])) for ticket, pr in work),
        key=lambda item: 2 if item.hold else int(item.kind != "pr"),
    )
    return _summarize(snapshot, history, items)
