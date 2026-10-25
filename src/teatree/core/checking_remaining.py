"""Read-only Remaining group for the checking report."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q

from teatree.core._checking_gather import _latest_failed_tasks, _merge_safe_prs, ticket_url
from teatree.core.admission_priority import ADMISSION_ORDER, admission_priority_annotations
from teatree.core.agent_admission import headless_lane_widths
from teatree.core.checking_types import DEFAULT_CAP, RemainingGroup, RemainingItem
from teatree.core.cycle_time.eta import format_minutes, queue_waves
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


def _task_matches_step(task: Task | None, step: str) -> bool:
    if task is None:
        return False
    phase = normalize_phase(task.phase)
    if step in {"coding", "rework"}:
        return phase in {"coding", "debugging"}
    if step == "factory review":
        return phase in REVIEW_PHASES
    return step == phase


def _running_measured(
    history: StepHistory, phase: str, estimate: StepEstimate, elapsed: float
) -> tuple[tuple[float, float] | None, str, str]:
    measured = history.remaining_measured(phase, elapsed)
    if measured is not None:
        note = f"{phase}: {measured[2]} elapsed survivors" if measured[2] != estimate.window else ""
        return measured[:2], "", note
    count = len(history.spans.get(phase, ()))
    reason = (
        f"running longer than all {count} 7d samples"
        if count > estimate.n
        else f"running longer than all {estimate.n} {estimate.window} samples"
    )
    return None, reason, ""


def _queue_delay(
    task: Task | None, phase: str, estimate: StepEstimate, queue: "QueueState"
) -> tuple[float, float] | str:
    queue_phase = task.phase if task is not None else phase
    lane = _lane(queue_phase)
    waves = queue_waves(ahead=queue.ahead(task, queue_phase), occupied=queue.occupied[lane], seats=queue.seats[lane])
    if waves is None:
        return f"no seats for {lane} lane"
    return waves * estimate.median_minutes, waves * estimate.p80_minutes


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

    def is_waiting(self, task: Task | None) -> bool:
        return bool(task and any(row.pk == task.pk for row in self.pending))

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
            pending_counts=Counter(normalize_phase(task.phase) for task in pending),
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
    running_since = (task.admitted_at or task.claimed_at) if task and task.status == Task.Status.CLAIMED else None
    for index, (step, phase) in enumerate(zip(steps, duration_phases, strict=True)):
        estimate = history.estimate(phase)
        if estimate is None:
            return {
                "median_minutes": None,
                "p80_minutes": None,
                "basis": "no default: no completed-task history",
            }, "ETA unknown (no default: no completed-task history)"
        if index == 0 and step not in {"merge", "deploy"} and running_since is not None:
            elapsed = max(0.0, (queue.now - running_since).total_seconds() / 60)
            if estimate.basis == "measured":
                remaining, reason, note = _running_measured(history, phase, estimate, elapsed)
                if remaining is None:
                    return {"median_minutes": None, "p80_minutes": None, "basis": reason}, f"ETA unknown ({reason})"
                median += remaining[0]
                p80 += remaining[1]
                defaults.add(note)
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
                delay = _queue_delay(task, phase, estimate, queue)
                if isinstance(delay, str):
                    return {"median_minutes": None, "p80_minutes": None, "basis": delay}, f"ETA unknown ({delay})"
                median += delay[0]
                p80 += delay[1]
            median += estimate.median_minutes
            p80 += estimate.p80_minutes
    defaults.discard("")
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
            .filter(task_scope, ticket__role=Ticket.Role.AUTHOR)
            .exclude(ticket__state__in=[Ticket.State.DELIVERED, Ticket.State.REVIEW_DELIVERED, Ticket.State.IGNORED])
            .select_related("ticket")
            .order_by("-pk")
        )
        all_active = list(
            Task.objects.filter(status__in=Task.Status.active()).select_related("ticket").order_by("created_at", "pk")
        )
        active = [task for task in all_active if overlay_names is None or task.ticket.overlay in overlay_names]
        owner_questions = list(
            DeferredQuestion.owner_pending()
            .filter(
                question_scope,
                parked_task__ticket__role=Ticket.Role.AUTHOR,
                parked_task__status=Task.Status.PENDING,
            )
            .select_related("parked_task")
        )
        recent_failed = _latest_failed_tasks(now, task_scope)
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
                all_active,
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
    step_task = task if steps and _task_matches_step(task, steps[0]) else None
    duration_phases = tuple(_duration_phase(step, step_task, index) for index, step in enumerate(steps))
    if hold:
        eta = {"median_minutes": None, "p80_minutes": None, "basis": hold}
        eta_text = f"ETA unknown ({hold})"
    else:
        eta, eta_text = _eta_for_item(
            steps=steps, duration_phases=duration_phases, task=step_task, history=history, queue=snapshot.queue
        )
    overlay_tag = f" [{ticket.overlay}]" if snapshot.tag_overlays else ""
    if step_task and step_task.status == Task.Status.CLAIMED:
        current = f"{steps[0]} (running)"
    elif step_task and (snapshot.queue.is_waiting(step_task) or snapshot.queue.has_seat(step_task)):
        ahead = snapshot.queue.ahead(step_task, step_task.phase)
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
        label = dict(FailureKind.choices).get(kind, kind).lower() if kind else ""
        return "halted", f"halted{f' ({label})' if label else ''} · "
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


def _summarize(
    snapshot: RemainingSnapshot, history: StepHistory, items: list[RemainingItem], cap: int
) -> RemainingGroup:
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
            f"{total_text} · {len(items)} {'item' if len(items) == 1 else 'items'}, "
            f"{unknown} unknown, {held['halted']} halted, "
            f"{held['blocked']} blocked, {owner_count} waiting on you, {snapshot.dormant} dormant"
            + (f" · basis: {legend}" if legend else "")
        ),
    )
    queue = snapshot.queue.to_dict()
    oldest = format_minutes(queue["oldest_pending_minutes"]) if snapshot.queue.oldest else "none"
    oldest_phase = f" {queue['oldest_pending_phase']}" if queue["oldest_pending_phase"] else ""
    queue_item = RemainingItem(
        label="Queue",
        url="",
        kind="queue",
        detail=(
            f"claimed {_format_counts(queue['claimed'])}; pending {_format_counts(queue['pending'])}; oldest "
            f"{oldest}{oldest_phase}; "
            f"seats {_format_counts(queue['seats'])}"
        ),
    )
    return RemainingGroup(
        title="Remaining",
        items=[summary, queue_item, *items[:cap]],
        total=len(items),
        eta=eta_total,
        queue=queue,
        durations=durations,
        holds={"halted": held["halted"], "blocked": held["blocked"], "owner": owner_count, "dormant": snapshot.dormant},
    )


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in sorted(counts.items())) or "none"


def gather_remaining(
    *, now: datetime, overlay_names: list[str] | None, tag_overlays: bool, cap: int = DEFAULT_CAP
) -> RemainingGroup:
    snapshot = RemainingSnapshot.load(now=now, overlay_names=overlay_names, tag_overlays=tag_overlays)
    active_by_ticket: dict[int, list[Task]] = defaultdict(list)
    for task in snapshot.active:
        active_by_ticket[task.ticket_id].append(task)
    pr_ticket_ids = {pr.ticket_id for pr in snapshot.prs}
    work: list[tuple[Ticket, PullRequest | None]] = [(pr.ticket, pr) for pr in snapshot.prs]
    work.extend((ticket, None) for ticket in snapshot.open_author if ticket.pk not in pr_ticket_ids)
    if not work and not snapshot.active and not snapshot.owner_questions:
        return empty_remaining(dormant=snapshot.dormant)
    history = StepHistory.measure(now) if work else StepHistory(now=now, spans={})
    items = sorted(
        (_work_item(snapshot, history, ticket, pr, active_by_ticket.get(ticket.pk, [])) for ticket, pr in work),
        key=lambda item: 2 if item.hold else int(item.kind != "pr"),
    )
    return _summarize(snapshot, history, items, cap)
