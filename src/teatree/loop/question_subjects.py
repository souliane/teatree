"""Which ticket(s) a pending ``DeferredQuestion`` is about (#4178).

The repair-loop reconcile (:mod:`teatree.loop.repair_halt_reconcile`) could only
answer that for a row carrying a ``repair-`` ``dedupe_marker`` — 6 of 70 pending
rows when #4178 was measured. The other 64 had no marker to key a subject off, so
no resolver could reach them and each waited on a human.

This is the generalised answer. Three sources, consulted in order, and the FIRST one
that is applicable to the row owns it:

1. the repair markers — a ``repair-`` prefixed row belongs to #3692's reconcile
    outright, delegated verbatim to
    :func:`~teatree.loop.repair_halt_reconcile.repair_marker_subject_tickets`.
2. ``parked_task`` — an explicit FK to the ``Task`` whose park raised the question,
    so its ticket IS the subject. The headless needs-input lane sets it.
3. ``session_id`` when it is a ``Session`` pk. The column holds EITHER a harness
    session UUID (the away-mode ``AskUserQuestion`` hook) or ``str(task.session_id)``
    from the task-derived producers. Only an all-digit value that resolves to a real
    ``Session`` row is accepted, so a harness UUID never derives a subject.

Applicability is why the answer is a typed three-way rather than ``list | None``.
The repair escalation stamps ``session_id=str(task.session_id)``, so every repair row
also carries source 3 — and a falsy ``no subject`` from source 1 would hand the row to
a source that answers about the ASKING session instead of the marker's own subjects.
An applicable source that cannot name a subject therefore STOPS the chain, which is
#3692's "the caller keeps its question" preserved through the generalisation.

The marker parse is deliberately NOT widened past the ``repair-`` prefixes: a
non-repair marker's second field is not a ticket pk (``attachment-hold:5``), and
reading it as one would drain a live owner question on a coincidence.

Every source answers ``None`` — undeterminable — rather than guessing, and the
sweep treats ``None`` as KEEP.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from teatree.core.models import PullRequest, Session, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.repair_halt_reconcile import repair_marker_subject_tickets

_REPAIR_PREFIX = "repair-"


@dataclass(frozen=True, slots=True)
class SubjectAnswer:
    """One source's answer about a question's subject, as ticket PKs.

    ``applicable`` is what a bare ``list[int] | None`` could not express: whether this
    source OWNS the row, independently of whether it managed to name a subject.

    PKs rather than FSM states because the state is only ONE of the facts a subject
    ticket carries: whether its pull requests are settled is another, and a ticket
    can sit non-terminal for weeks with its PR already merged.
    """

    applicable: bool
    tickets: tuple[int, ...] = ()


NOT_APPLICABLE = SubjectAnswer(applicable=False)
UNDETERMINABLE = SubjectAnswer(applicable=True)


def _resolved(tickets: Sequence[int]) -> SubjectAnswer:
    return SubjectAnswer(applicable=True, tickets=tuple(tickets))


@dataclass(frozen=True, slots=True)
class SubjectIndex:
    """Pre-resolved subject states for one sweep's pending rows.

    Built once per sweep so a backlog of N rows costs three queries rather than 3N.
    """

    #: ``repair-`` dedupe marker -> the pks of the tickets that raised it.
    marker_tickets: dict[str, list[int]]
    #: question pk -> its parked task's ticket pk, for the rows that carry one.
    parked_task_tickets: dict[int, int]
    session_tickets: dict[int, int]
    ticket_states: dict[int, str]
    #: subject ticket pk -> the state of every pull request recorded against it.
    ticket_pr_states: dict[int, tuple[str, ...]]

    @classmethod
    def build(cls, questions: Sequence[DeferredQuestion]) -> "SubjectIndex":
        markers = repair_marker_subject_tickets(
            {q.dedupe_marker for q in questions if q.dedupe_marker.startswith(_REPAIR_PREFIX)}
        )
        parked = _parked_ticket_ids({q.pk for q in questions})
        sessions = _session_ticket_ids(_session_pks(questions))

        subjects = {pk for pks in markers.values() for pk in pks} | set(parked.values()) | set(sessions.values())
        return cls(
            marker_tickets=markers,
            parked_task_tickets=parked,
            session_tickets=sessions,
            ticket_states=_ticket_states(subjects),
            ticket_pr_states=_ticket_pr_states(subjects),
        )

    def ticket_ids_for(self, question: DeferredQuestion) -> list[int] | None:
        """Every subject ticket's pk, or ``None`` when no source can name one."""
        for source in _SUBJECT_SOURCES:
            answer = source(self, question)
            if answer.applicable:
                return list(answer.tickets) or None
        return None

    def states_for(self, question: DeferredQuestion) -> list[str] | None:
        """Every subject ticket's FSM state, or ``None`` when no source can name one."""
        tickets = self.ticket_ids_for(question)
        if tickets is None:
            return None
        return [self.ticket_states[pk] for pk in tickets if pk in self.ticket_states] or None

    def pr_states_for(self, question: DeferredQuestion) -> list[str] | None:
        """Every pull-request state the subject tickets carry, or ``None`` when undeterminable.

        ``None`` covers both "no subject" and "a subject ticket has no pull request at
        all": a ticket with no PR row proves nothing about whether its work landed, so
        a caller reading this must never drain on it. Partial evidence is no evidence —
        one un-PR'd subject voids the whole answer rather than shrinking it.
        """
        tickets = self.ticket_ids_for(question)
        if not tickets:
            return None
        states: list[str] = []
        for pk in tickets:
            recorded = self.ticket_pr_states.get(pk)
            if not recorded:
                return None
            states.extend(recorded)
        return states


SubjectSource = Callable[[SubjectIndex, DeferredQuestion], SubjectAnswer]


def _repair_marker_answer(index: SubjectIndex, question: DeferredQuestion) -> SubjectAnswer:
    if not question.dedupe_marker.startswith(_REPAIR_PREFIX):
        return NOT_APPLICABLE
    tickets = index.marker_tickets.get(question.dedupe_marker)
    return _resolved(tickets) if tickets else UNDETERMINABLE


def _parked_task_answer(index: SubjectIndex, question: DeferredQuestion) -> SubjectAnswer:
    ticket = index.parked_task_tickets.get(question.pk)
    return _resolved([ticket]) if ticket is not None else NOT_APPLICABLE


def _session_answer(index: SubjectIndex, question: DeferredQuestion) -> SubjectAnswer:
    ticket = index.session_tickets.get(_session_pk(question) or 0)
    return _resolved([ticket]) if ticket is not None else NOT_APPLICABLE


#: Consulted in order; the first APPLICABLE source owns the row (see the module docstring).
_SUBJECT_SOURCES: tuple[SubjectSource, ...] = (_repair_marker_answer, _parked_task_answer, _session_answer)


def _session_pk(question: DeferredQuestion) -> int | None:
    """The ``Session`` pk *question* names, or ``None`` for a harness UUID / blank."""
    return int(question.session_id) if question.session_id.isdigit() else None


def _session_pks(questions: Sequence[DeferredQuestion]) -> set[int]:
    return {pk for pk in (_session_pk(q) for q in questions) if pk is not None}


def _parked_ticket_ids(question_pks: set[int]) -> dict[int, int]:
    if not question_pks:
        return {}
    return dict(
        DeferredQuestion.objects.filter(pk__in=question_pks, parked_task__isnull=False).values_list(
            "pk", "parked_task__ticket_id"
        )
    )


def _session_ticket_ids(session_pks: set[int]) -> dict[int, int]:
    if not session_pks:
        return {}
    return dict(Session.objects.filter(pk__in=session_pks).values_list("pk", "ticket_id"))


def _ticket_states(ticket_pks: set[int]) -> dict[int, str]:
    if not ticket_pks:
        return {}
    return dict(Ticket.objects.filter(pk__in=ticket_pks).values_list("pk", "state"))


def _ticket_pr_states(ticket_pks: set[int]) -> dict[int, tuple[str, ...]]:
    if not ticket_pks:
        return {}
    grouped: dict[int, list[str]] = defaultdict(list)
    for ticket_pk, state in PullRequest.objects.filter(ticket_id__in=ticket_pks).values_list("ticket_id", "state"):
        grouped[ticket_pk].append(state)
    return {pk: tuple(states) for pk, states in grouped.items()}
