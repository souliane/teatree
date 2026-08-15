"""The registry of automated drains over the pending ``DeferredQuestion`` backlog (#4178).

One automated drain existed before this — the ``repair-`` prefixed reconcile — and it
reached 6 of 70 pending rows. The other 64 waited on a human answering one at a time,
46 of them for three days or more, which is owner directive #45 ("an unresolved request
is never silently dropped") failing in the slow direction.

The sweep runs in two stages, and the split is the whole design:

* **subject stage** — resolvers that can name the question's SUBJECT and read its state.
    ``DRAIN`` only when every subject ticket is terminal (the answer could then only ever
    be "ignore"); ``KEEP`` when one is still live; no decision at all when no subject is
    derivable. The conservatism guard #3692 established is unchanged — a live or
    undeterminable subject is never dropped.
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
from teatree.core.models import Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_subjects import SubjectIndex


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

    @classmethod
    def build(cls, questions: Sequence[DeferredQuestion]) -> "SweepContext":
        return cls(
            index=SubjectIndex.build(questions),
            now=timezone.now(),
            ceiling_days=int(get_effective_settings().deferred_question_age_ceiling_days),
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


#: Resolvers that decide from the question's SUBJECT — the only stage that may drain.
SUBJECT_RESOLVERS: tuple[tuple[str, Resolver], ...] = (("subject_terminal", _subject_terminal),)
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
