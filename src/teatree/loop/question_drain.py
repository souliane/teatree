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
    records an escalation, which is a state transition and never a resolution.

:func:`question_reachability` is the measurement the issue asked for: per pending row,
which resolvers can decide it right now. A row no resolver can decide is the gap.

Lives in ``teatree.loop`` (orchestration): it composes the ``DeferredQuestion`` domain
model with the subject derivation and the effective-settings read, and it is driven from
the tick recovery sweep.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
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


Resolver = Callable[[DeferredQuestion, SubjectIndex], Decision | None]


def _subject_terminal(question: DeferredQuestion, index: SubjectIndex) -> Decision | None:
    states = index.states_for(question)
    if not states:
        return None
    if all(state in Ticket._TERMINAL_STATES for state in states):  # noqa: SLF001 — model SSOT terminal set
        return Decision(Verdict.DRAIN, f"every subject ticket is terminal ({', '.join(sorted(set(states)))})")
    return Decision(Verdict.KEEP, "a subject ticket is still live")


def _age_ceiling(question: DeferredQuestion, index: SubjectIndex) -> Decision | None:  # noqa: ARG001 — registry signature
    ceiling_days = int(get_effective_settings().deferred_question_age_ceiling_days)
    if ceiling_days <= 0:
        return None
    window = timedelta(days=ceiling_days)
    cutoff = timezone.now() - window
    if question.created_at > cutoff:
        return None
    if question.escalated_at is not None and question.escalated_at > cutoff:
        return None
    return Decision(Verdict.ESCALATE, f"pending past the {ceiling_days}d ceiling with no resolution")


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
    index = SubjectIndex.build(pending)
    drained = escalated = 0
    for question in pending:
        subject = _first_decision(SUBJECT_RESOLVERS, question, index)
        if subject is not None and subject.verdict is Verdict.DRAIN:
            question.mark_stale(subject.reason)
            drained += 1
            continue
        backstop = _first_decision(BACKSTOP_RESOLVERS, question, index)
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
    index = SubjectIndex.build(pending)
    return [
        QuestionReach(
            question_id=question.pk,
            has_subject=index.states_for(question) is not None,
            decisions={
                name: decision.verdict
                for name, resolver in (*SUBJECT_RESOLVERS, *BACKSTOP_RESOLVERS)
                if (decision := resolver(question, index)) is not None
            },
        )
        for question in pending
    ]


def _first_decision(
    resolvers: Sequence[tuple[str, Resolver]], question: DeferredQuestion, index: SubjectIndex
) -> Decision | None:
    for _name, resolver in resolvers:
        decision = resolver(question, index)
        if decision is not None:
            return decision
    return None
