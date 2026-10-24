"""Read-only Remaining group for the checking report."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from teatree.core._checking_gather import ticket_url
from teatree.core.agent_admission import headless_lane_widths
from teatree.core.checking_types import CheckGroup, CheckGroupDict, CheckItem, CheckItemDict
from teatree.core.cycle_time.eta import format_minutes, queue_waves, remaining_given_elapsed
from teatree.core.cycle_time.step_history import StepHistory
from teatree.core.lifecycle_pipeline import PIPELINE, TicketSnapshot, resolve_plan
from teatree.core.modelkit.phases import REVIEW_PHASES, CheapClass, normalize_phase
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket

STEP_PHASE = {
    "rework": "coding",
    "factory review": "reviewing",
    "independent review": "reviewing",
}
POST_PR = ("independent review", "merge", "deploy")
PHASE_LABEL = {"reviewing": "factory review", "coding": "coding"}


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


def empty_remaining() -> RemainingGroup:
    return RemainingGroup(
        title="Remaining",
        eta={
            "median_minutes": None,
            "p80_minutes": None,
            "known": 0,
            "unknown": 0,
            "basis": "no current work in scope",
        },
        queue={"claimed": {}, "pending": {}, "oldest_pending_minutes": None, "oldest_pending_phase": None, "seats": {}},
        holds={"halted": 0, "blocked": 0, "owner": 0, "dormant": 0},
    )


def remaining_steps(state: str, active_phase: str, *, has_pr: bool, merge_safe: bool) -> tuple[str, ...]:
    phase = normalize_phase(active_phase)
    if state in {Ticket.State.MERGED, Ticket.State.RETRO_RECORDED}:
        return ("deploy",)
    if has_pr:
        if phase in {"coding", "debugging"}:
            before = ("rework", "testing", "factory review")
        elif phase == "testing":
            before = ("testing", "factory review")
        elif phase in REVIEW_PHASES:
            before = ("factory review",)
        elif merge_safe:
            before = ()
            return ("merge", "deploy")
        else:
            before = ()
        return (*before, *POST_PR)
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
        if estimate.basis != "measured":
            defaults.add(f"{phase}: {estimate.basis}")
        if (
            index == 0
            and step not in {"merge", "deploy"}
            and task is not None
            and task.status == Task.Status.CLAIMED
            and task.admitted_at
        ):
            elapsed = max(0.0, (queue.now - task.admitted_at).total_seconds() / 60)
            remaining = remaining_given_elapsed(estimate.samples, elapsed)
            remaining_p80 = remaining_given_elapsed(estimate.p80_samples, elapsed)
            if remaining is None or remaining_p80 is None:
                reason = f"running longer than all {estimate.n} {estimate.window} samples"
                return {"median_minutes": None, "p80_minutes": None, "basis": reason}, f"ETA unknown ({reason})"
            median += remaining[0]
            p80 += remaining_p80[1]
        else:
            if index == 0 and step not in {"merge", "deploy"}:
                lane = _lane(phase)
                ahead = sum(
                    _lane(row.phase) == lane
                    and (
                        task is None
                        or (cast("datetime", row.created_at), row.pk) < (cast("datetime", task.created_at), task.pk)
                    )
                    for row in queue.pending
                    if row.pk != (task.pk if task else None)
                )
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
    recent_failed: dict[int, str]
    open_author: list[Ticket]
    verdicts: set[tuple[str, int]]
    queue: QueueState
    history: StepHistory

    @classmethod
    def load(cls, *, now: datetime, overlay_names: list[str], tag_overlays: bool) -> "RemainingSnapshot":
        prs = list(
            PullRequest.objects.live()
            .filter(ticket__overlay__in=overlay_names)
            .select_related("ticket")
            .order_by("-pk")
        )
        active = list(
            Task.objects.filter(status__in=Task.Status.active(), ticket__overlay__in=overlay_names)
            .select_related("ticket")
            .order_by("created_at", "pk")
        )
        owner_questions = list(
            DeferredQuestion.owner_pending()
            .filter(
                parked_task__ticket__overlay__in=overlay_names,
                parked_task__ticket__role=Ticket.Role.AUTHOR,
                parked_task__status=Task.Status.PENDING,
            )
            .select_related("parked_task")
        )
        latest_tasks: dict[int, tuple[int, str]] = {}
        for task_id, ticket_id, status in (
            Task.objects.filter(ticket__overlay__in=overlay_names)
            .order_by("ticket_id", "-pk")
            .values_list("pk", "ticket_id", "status")
        ):
            latest_tasks.setdefault(ticket_id, (task_id, status))
        failed_ids = [task_id for task_id, status in latest_tasks.values() if status == Task.Status.FAILED]
        recent_failed: dict[int, str] = {}
        seen_attempts: set[int] = set()
        for task_id, ticket_id, ended, failure_kind in (
            TaskAttempt.objects.filter(task_id__in=failed_ids)
            .order_by("task_id", "-ended_at", "-pk")
            .values_list("task_id", "task__ticket_id", "ended_at", "failure_kind")
        ):
            if task_id in seen_attempts:
                continue
            seen_attempts.add(task_id)
            if ended and ended >= now - timedelta(days=7):
                recent_failed[ticket_id] = str(failure_kind)
        open_author = list(
            Ticket.objects.filter(overlay__in=overlay_names, role=Ticket.Role.AUTHOR)
            .exclude(state__in=[Ticket.State.DELIVERED, Ticket.State.REVIEW_DELIVERED, Ticket.State.IGNORED])
            .order_by("-pk")
        )
        heads: dict[tuple[str, int], str] = {}
        holds: set[tuple[str, int]] = set()
        passes: set[tuple[str, int]] = set()
        for slug, pr_id, reviewed_sha, verdict in (
            ReviewVerdict.objects.filter(pr_id__in=[int(pr.iid) for pr in prs if pr.iid.isdigit()])
            .order_by("-recorded_at", "-pk")
            .values_list("slug", "pr_id", "reviewed_sha", "verdict")
        ):
            key = slug.casefold(), pr_id
            head = reviewed_sha.strip().lower()
            if heads.setdefault(key, head) != head:
                continue
            if verdict == ReviewVerdict.Verdict.MERGE_SAFE:
                passes.add(key)
            else:
                holds.add(key)
        verdicts = passes - holds
        return cls(
            now=now,
            tag_overlays=tag_overlays,
            prs=prs,
            active=active,
            owner_questions=owner_questions,
            recent_failed=recent_failed,
            open_author=open_author,
            verdicts=verdicts,
            queue=QueueState.from_tasks(
                list(Task.objects.filter(status__in=Task.Status.active())),
                list(Task.objects.waiting_for_admission()),
                now,
            ),
            history=StepHistory.measure(now),
        )


def _work_item(
    snapshot: RemainingSnapshot,
    ticket: Ticket,
    pr: PullRequest | None,
    tasks: list[Task],
    owner_task_ids: set[int],
) -> RemainingItem:
    task = next((row for row in tasks if row.status == Task.Status.CLAIMED), tasks[0] if tasks else None)
    hold, status = _hold_for_item(snapshot, ticket, tasks, owner_task_ids)
    merge_safe = bool(pr and pr.iid.isdigit() and (pr.repo.casefold(), int(pr.iid)) in snapshot.verdicts)
    steps = remaining_steps(ticket.state, task.phase if task else "", has_pr=pr is not None, merge_safe=merge_safe)
    duration_phases = tuple(
        normalize_phase(task.phase)
        if index == 0 and task is not None and step not in {"merge", "deploy"}
        else STEP_PHASE.get(step, step)
        for index, step in enumerate(steps)
    )
    if hold:
        eta = {"median_minutes": None, "p80_minutes": None, "basis": hold}
        eta_text = f"ETA unknown ({hold})"
    else:
        eta, eta_text = _eta_for_item(
            steps=steps, duration_phases=duration_phases, task=task, history=snapshot.history, queue=snapshot.queue
        )
    overlay_tag = f" [{ticket.overlay}]" if snapshot.tag_overlays else ""
    if task and task.status == Task.Status.CLAIMED:
        current = f"{steps[0]} (running)"
    elif task:
        lane = _lane(task.phase)
        ahead = sum(
            _lane(row.phase) == lane
            and (cast("datetime", row.created_at), row.pk) < (cast("datetime", task.created_at), task.pk)
            for row in snapshot.queue.pending
            if row.pk != task.pk
        )
        current = f"{steps[0]} (queued, {ahead} ahead)"
    else:
        current = steps[0]
    detail = f"{status}now: {current}; then {', '.join(steps[1:])} · {eta_text}{overlay_tag}"
    return RemainingItem(
        label=f"{pr.repo}#{pr.iid}" if pr else f"#{ticket.ticket_number}",
        url=pr.url if pr else ticket_url(ticket),
        title=ticket.short_description or "work in progress",
        detail=detail,
        kind="pr" if pr else "ticket",
        steps=steps,
        eta=eta,
        duration_phases=duration_phases,
    )


def _hold_for_item(
    snapshot: RemainingSnapshot, ticket: Ticket, tasks: list[Task], owner_task_ids: set[int]
) -> tuple[str, str]:
    if tasks and all(row.pk in owner_task_ids for row in tasks):
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


def _summarize(snapshot: RemainingSnapshot, items: list[RemainingItem], dormant: int) -> RemainingGroup:
    known = [item.eta for item in items if item.eta["median_minutes"] is not None]
    unknown = sum(
        item.eta["median_minutes"] is None and item.eta["basis"] not in {"halted", "blocked", "waiting on you"}
        for item in items
    )
    held = Counter(item.eta["basis"] for item in items if item.eta["basis"] in {"halted", "blocked", "waiting on you"})
    unknown_bases = {
        item.eta["basis"]
        for item in items
        if item.eta["median_minutes"] is None and item.eta["basis"] not in {"halted", "blocked", "waiting on you"}
    }
    if known:
        basis = "maximum known item range; unknown and held items excluded"
    elif len(unknown_bases) == 1:
        basis = next(iter(unknown_bases))
    elif unknown_bases:
        basis = "multiple unknown ETA reasons"
    elif items:
        basis = "all current work is held"
    else:
        basis = "no current work in scope"
    median_total = max((float(row["median_minutes"]) for row in known), default=None)
    p80_total = max((float(row["p80_minutes"]) for row in known), default=None)
    eta_total = {
        "median_minutes": median_total,
        "p80_minutes": p80_total,
        "known": len(known),
        "unknown": unknown,
        "basis": basis,
    }
    total_text = (
        f"ETA {format_minutes(median_total)}\u2013{format_minutes(p80_total)}"
        if median_total is not None and p80_total is not None
        else f"ETA unknown ({basis})"
    )
    owner_count = held["waiting on you"]
    durations = [
        _duration_dict(snapshot.history, step)
        for step in sorted({phase for item in items for phase in item.duration_phases})
    ]
    legend = "; ".join(
        f"{row['step']} {row['median_minutes']}\u2013{row['p80_minutes']}m n={row['n']} {row['window']} "
        f"{row['source']} ({row['basis']})"
        for row in durations
    )
    summary = RemainingItem(
        label="Total",
        url="",
        kind="summary",
        detail=(
            f"{total_text} · {len(items)} items, {unknown} unknown, {held['halted']} halted, "
            f"{owner_count} waiting on you, {dormant} dormant" + (f" · basis: {legend}" if legend else "")
        ),
    )
    queue = snapshot.queue.to_dict()
    queue_item = RemainingItem(
        label="Queue",
        url="",
        kind="queue",
        detail=(
            f"claimed {queue['claimed']}; pending {queue['pending']}; oldest "
            f"{format_minutes(queue['oldest_pending_minutes']) if snapshot.queue.oldest else 'none'} "
            f"{queue['oldest_pending_phase'] or ''}; "
            f"seats {queue['seats']}"
        ),
    )
    return RemainingGroup(
        title="Remaining",
        items=[summary, queue_item, *items],
        total=len(items) + 2,
        eta=eta_total,
        queue=queue,
        durations=durations,
        holds={"halted": held["halted"], "blocked": held["blocked"], "owner": owner_count, "dormant": dormant},
    )


def gather_remaining(*, now: datetime, overlay_names: list[str], tag_overlays: bool) -> RemainingGroup:
    snapshot = RemainingSnapshot.load(now=now, overlay_names=overlay_names, tag_overlays=tag_overlays)
    active_by_ticket: dict[int, list[Task]] = defaultdict(list)
    for task in snapshot.active:
        active_by_ticket[task.ticket_id].append(task)
    owner_ticket_ids = {
        parked_task.ticket_id
        for question in snapshot.owner_questions
        if (parked_task := question.parked_task) is not None
    }
    owner_task_ids = {row.parked_task_id for row in snapshot.owner_questions if row.parked_task_id is not None}
    pr_ticket_ids = {pr.ticket_id for pr in snapshot.prs}
    relevant = pr_ticket_ids | active_by_ticket.keys() | owner_ticket_ids | snapshot.recent_failed.keys()
    work: list[tuple[Ticket, PullRequest | None]] = [
        (pr.ticket, pr) for pr in snapshot.prs if pr.ticket.role == Ticket.Role.AUTHOR
    ]
    work.extend(
        (ticket, None) for ticket in snapshot.open_author if ticket.pk in relevant and ticket.pk not in pr_ticket_ids
    )
    dormant = sum(
        ticket.pk not in relevant and ticket.state not in Ticket.finished_states() for ticket in snapshot.open_author
    )
    if not work and not snapshot.active and not snapshot.owner_questions:
        return empty_remaining()
    items = [
        _work_item(snapshot, ticket, pr, active_by_ticket.get(ticket.pk, []), owner_task_ids) for ticket, pr in work
    ]
    return _summarize(snapshot, items, dormant)
