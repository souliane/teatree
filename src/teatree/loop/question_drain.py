"""The registry of automated drains over the pending ``DeferredQuestion`` backlog (#4178).

One automated drain existed before this — the ``repair-`` prefixed reconcile — and it
reached 6 of 70 pending rows. The other 64 waited on a human answering one at a time,
46 of them for three days or more, which is owner directive #45 ("an unresolved request
is never silently dropped") failing in the slow direction.

The sweep runs in two stages, and the split is the whole design:

* **subject stage** — resolvers that can name the question's SUBJECT and read its
    state. ``DRAIN`` only when every subject ticket is terminal (the answer could then
    only ever be "ignore"); ``KEEP`` when one is still live; no decision at all when no
    subject is derivable. The conservatism guard #3692 established is unchanged — a live
    or undeterminable subject is never dropped. Two later resolvers read facts the FSM
    state cannot carry — a subject whose pull requests have all settled, and a parked
    lane that has since re-run to completion — and both are POSITIVE-ONLY: short of
    proof they answer nothing at all, so they add drains without ever suppressing one.
* **backstop stage** — runs on every row the subject stage did NOT drain, including one
    it explicitly kept, so a KEEP is not a licence to sit forever. Past the age ceiling it
    records an escalation, which is a state transition and never a resolution. The stamp
    is rendered by :func:`~teatree.core.notify_question_drains.format_backlog_digest` and
    ``t3 <overlay> questions list``, so the escalation reaches the owner.

Both stages read one :class:`SweepContext`, built once per sweep.

:func:`question_reachability` is the measurement the issue asked for: per pending row,
which resolvers can decide it right now. A row no resolver can decide is the gap.

Lives in ``teatree.loop`` (orchestration): it composes the ``DeferredQuestion`` domain
model with the subject derivation and the effective-settings read, and it is driven from
the tick recovery sweep.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.utils import timezone

from teatree.config.resolution import get_effective_settings
from teatree.core.models import PullRequest, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_subjects import SubjectIndex

#: The pull-request states that are DONE either way — the same partition
#: ``PullRequestQuerySet.live()`` excludes. A merge succeeded and a close was given
#: up on; both mean nothing further will be asked of the question that guarded it.
_SETTLED_PR_STATES: frozenset[str] = frozenset({PullRequest.State.MERGED, PullRequest.State.CLOSED})


class Verdict(StrEnum):
    DRAIN = "drain"
    KEEP = "keep"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    reason: str


@dataclass(frozen=True, slots=True)
class QuestionReach:
    """Which resolvers can decide one pending row right now."""

    question_id: int
    has_subject: bool
    decisions: dict[str, Verdict]


@dataclass(frozen=True, slots=True)
class DrainReport:
    drained: int
    escalated: int


@dataclass(frozen=True, slots=True)
class SweepContext:
    """Everything one sweep resolves ONCE, shared by every resolver over every row.

    Both hoists are correctness, not just cost: the settings read is 2 queries a row
    (``cached_per_request`` is inert off the HTTP path), and a per-row clock read moves
    the age cutoff WITHIN one sweep, so two equally-old rows could decide differently.
    """

    index: SubjectIndex
    now: datetime
    ceiling_days: int
    #: pks of the parked rows whose lane has since re-run to completion without them.
    superseded_parked: frozenset[int]

    @classmethod
    def build(cls, questions: Sequence[DeferredQuestion]) -> "SweepContext":
        return cls(
            index=SubjectIndex.build(questions),
            now=timezone.now(),
            ceiling_days=int(get_effective_settings().deferred_question_age_ceiling_days),
            superseded_parked=_superseded_parked_questions(questions),
        )


Resolver = Callable[[DeferredQuestion, SweepContext], Decision | None]


def _subject_terminal(question: DeferredQuestion, context: SweepContext) -> Decision | None:
    states = context.index.states_for(question)
    if not states:
        return None
    if all(state in Ticket._TERMINAL_STATES for state in states):  # noqa: SLF001 — model SSOT terminal set
        return Decision(Verdict.DRAIN, f"every subject ticket is terminal ({', '.join(sorted(set(states)))})")
    return Decision(Verdict.KEEP, "a subject ticket is still live")


def _age_ceiling(question: DeferredQuestion, context: SweepContext) -> Decision | None:
    if context.ceiling_days <= 0:
        return None
    cutoff = context.now - timedelta(days=context.ceiling_days)
    if question.created_at > cutoff:
        return None
    if question.escalated_at is not None and question.escalated_at > cutoff:
        return None
    return Decision(Verdict.ESCALATE, f"pending past the {context.ceiling_days}d ceiling with no resolution")


def _parked_task_superseded(question: DeferredQuestion, context: SweepContext) -> Decision | None:
    """The parked lane has since re-run to completion WITHOUT this answer — DRAIN.

    ``parked_task`` is the task whose ``needs_user_input`` stop raised the question,
    and its own status proves nothing: the park runs inside ``Task._advance_ticket``,
    which is reached only AFTER the task is stamped ``COMPLETED``, so every parked
    task is already completed the moment its question is written. A resolver keyed on
    that alone would drain the entire parked backlog on its first tick — which is why
    this one reads supersession instead (see :func:`_superseded_parked_questions` for
    the four guards it has to clear).

    Positive-only: it can only ever ADD a drain, never veto one, so its position ahead
    of :func:`_subject_terminal` cannot change an existing verdict.
    """
    if question.pk not in context.superseded_parked:
        return None
    return Decision(Verdict.DRAIN, "the parked phase has since completed a newer run without this answer")


def _pr_terminal(question: DeferredQuestion, context: SweepContext) -> Decision | None:
    """Every pull request the subject produced is settled — DRAIN; anything else, no decision.

    A narrower fact than :func:`_subject_terminal` and a strictly later one: a ticket
    can sit at ``reviewed`` for weeks with its PR already merged, so the FSM state says
    "live" about work that has landed. Where the PRs are all merged or closed, the
    question that guarded them can no longer change anything.

    Positive-only, deliberately. A subject with no PR row at all, or one PR still open,
    returns ``None`` — the #3692 guard: uncertainty is KEEP, and this resolver may add
    a drain the ticket state cannot prove but must never suppress one it can.
    """
    states = context.index.pr_states_for(question)
    if not states or not all(state in _SETTLED_PR_STATES for state in states):
        return None
    settled = ", ".join(sorted(set(states)))
    return Decision(Verdict.DRAIN, f"every pull request recorded for the subject is settled ({settled})")


def _superseded_parked_questions(questions: Sequence[DeferredQuestion]) -> frozenset[int]:
    """The parked rows whose ``(ticket, phase)`` lane has since completed a NEWER run.

    Resolved once per sweep, in three queries, because the per-row answer needs the
    LATEST completed task of each lane rather than any of them. Four guards, each of
    which fails toward keeping the question:

    * there IS a later completed run of the same ``(ticket, phase)`` — no newer run
        means the lane is still where the question left it;
    * it is not a resume chained off the parked task itself, which only exists once
        the question has been answered;
    * it carries no pending question of its own — a newer run that ALSO parked is the
        lane repeating itself, not moving past it;
    * it was created after the question, so an in-flight sibling that happened to
        finish later cannot stand in for a re-run.
    """
    parked = {q.pk: q for q in questions if q.parked_task_id is not None}  # ty: ignore[unresolved-attribute]
    if not parked:
        return frozenset()

    tasks = {
        task.pk: task
        for task in Task.objects.filter(pk__in={q.parked_task_id for q in parked.values()})  # ty: ignore[unresolved-attribute]
    }
    still_parked = set(
        DeferredQuestion.objects.filter(
            answered_at__isnull=True, dismissed_at__isnull=True, parked_task__isnull=False
        ).values_list("parked_task_id", flat=True)
    )
    latest_run: dict[tuple[int, str], Task] = {}
    for task in Task.objects.filter(
        ticket_id__in={t.ticket_id for t in tasks.values()},
        status=Task.Status.COMPLETED,
    ):
        lane = (task.ticket_id, task.phase)
        if lane not in latest_run or task.pk > latest_run[lane].pk:
            latest_run[lane] = task

    return frozenset(
        pk
        for pk, question in parked.items()
        if _lane_moved_on(question, tasks.get(question.parked_task_id), latest_run, still_parked)  # ty: ignore[unresolved-attribute]
    )


def _lane_moved_on(
    question: DeferredQuestion,
    parked: Task | None,
    latest_run: dict[tuple[int, str], Task],
    still_parked: set[int],
) -> bool:
    if parked is None:
        return False
    newest = latest_run.get((parked.ticket_id, parked.phase))  # ty: ignore[unresolved-attribute]
    if newest is None or newest.pk <= parked.pk or newest.pk in still_parked:
        return False
    if newest.parent_task_id == parked.pk:  # ty: ignore[unresolved-attribute]
        return False
    return newest.created_at is not None and newest.created_at > question.created_at


#: Resolvers that decide from the question's SUBJECT — the only stage that may drain.
#: The two positive-only resolvers run FIRST: each answers ``None`` on anything short
#: of proof, so they can only add a drain to what ``subject_terminal`` already decides.
SUBJECT_RESOLVERS: tuple[tuple[str, Resolver], ...] = (
    ("parked_task_superseded", _parked_task_superseded),
    ("pr_terminal", _pr_terminal),
    ("subject_terminal", _subject_terminal),
)
#: Resolvers that guarantee a state transition on a row the subject stage left pending.
BACKSTOP_RESOLVERS: tuple[tuple[str, Resolver], ...] = (("age_ceiling", _age_ceiling),)


def drain_pending_questions() -> DrainReport:
    """Resolve what is mechanically decidable in the pending backlog; escalate the rest.

    Drains a row only on a ``DRAIN`` verdict from the subject stage, and escalates any
    remaining row the backstop stage rules past the ceiling. Idempotent: ``mark_stale``
    and ``mark_escalated`` are single-use CAS writes, and the escalation window keeps a
    re-tick inside the ceiling from re-stamping.
    """
    pending = list(DeferredQuestion.pending())
    if not pending:
        return DrainReport(drained=0, escalated=0)
    context = SweepContext.build(pending)
    drained = escalated = 0
    for question in pending:
        subject = _first_decision(SUBJECT_RESOLVERS, question, context)
        if subject is not None and subject.verdict is Verdict.DRAIN:
            question.mark_stale(subject.reason)
            drained += 1
            continue
        backstop = _first_decision(BACKSTOP_RESOLVERS, question, context)
        if backstop is not None and question.mark_escalated(backstop.reason):
            escalated += 1
    return DrainReport(drained=drained, escalated=escalated)


def question_reachability() -> list[QuestionReach]:
    """Per pending row, the verdict every registered resolver can reach right now.

    An empty ``decisions`` map is the #4178 gap made visible: no automated resolver can
    say anything about that row, so it can only ever be cleared by a human.
    """
    pending = list(DeferredQuestion.pending())
    if not pending:
        return []
    context = SweepContext.build(pending)
    return [
        QuestionReach(
            question_id=question.pk,
            has_subject=context.index.states_for(question) is not None,
            decisions={
                name: decision.verdict
                for name, resolver in (*SUBJECT_RESOLVERS, *BACKSTOP_RESOLVERS)
                if (decision := resolver(question, context)) is not None
            },
        )
        for question in pending
    ]


def _first_decision(
    resolvers: Sequence[tuple[str, Resolver]], question: DeferredQuestion, context: SweepContext
) -> Decision | None:
    for _name, resolver in resolvers:
        decision = resolver(question, context)
        if decision is not None:
            return decision
    return None
