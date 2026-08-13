"""Which ticket(s) a pending ``DeferredQuestion`` is about (#4178).

The repair-loop reconcile (:mod:`teatree.loop.repair_halt_reconcile`) could only
answer that for a row carrying a ``repair-`` ``dedupe_marker`` — 6 of 70 pending
rows when #4178 was measured. The other 64 had no marker to key a subject off, so
no resolver could reach them and each waited on a human.

This is the generalised answer. Three sources, consulted in order, and the FIRST one
that is applicable to the row owns it:

1. the repair markers — a ``repair-`` prefixed row belongs to #3692's reconcile
    outright, delegated verbatim to
    :func:`~teatree.loop.repair_halt_reconcile.repair_marker_subject_states`.
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

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from teatree.core.models import Session
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.repair_halt_reconcile import halt_marker_subject_states, repair_marker_subject_states

_HALT_PREFIX = "repair-halt:"
_REPAIR_PREFIX = "repair-"


@dataclass(frozen=True, slots=True)
class SubjectAnswer:
    """One source's answer about a question's subject.

    ``applicable`` is what a bare ``list[str] | None`` could not express: whether this
    source OWNS the row, independently of whether it managed to name a subject.
    """

    applicable: bool
    states: tuple[str, ...] = ()


NOT_APPLICABLE = SubjectAnswer(applicable=False)
UNDETERMINABLE = SubjectAnswer(applicable=True)


def _resolved(states: Sequence[str]) -> SubjectAnswer:
    return SubjectAnswer(applicable=True, states=tuple(states))


@dataclass(frozen=True, slots=True)
class SubjectIndex:
    """Pre-resolved subject states for one sweep's pending rows.

    Built once per sweep so a backlog of N rows costs three queries rather than 3N.
    """

    halt_markers: dict[str, list[str]]
    #: question pk -> its parked task's ticket state, for the rows that carry one.
    parked_task_states: dict[int, str]
    session_states: dict[int, str]

    @classmethod
    def build(cls, questions: Sequence[DeferredQuestion]) -> "SubjectIndex":
        return cls(
            halt_markers=halt_marker_subject_states(
                {q.dedupe_marker for q in questions if q.dedupe_marker.startswith(_HALT_PREFIX)}
            ),
            parked_task_states=_parked_ticket_states({q.pk for q in questions}),
            session_states=_session_ticket_states(_session_pks(questions)),
        )

    def states_for(self, question: DeferredQuestion) -> list[str] | None:
        """Every subject ticket's FSM state, or ``None`` when no source can name one."""
        for source in _SUBJECT_SOURCES:
            answer = source(self, question)
            if answer.applicable:
                return list(answer.states) or None
        return None


SubjectSource = Callable[[SubjectIndex, DeferredQuestion], SubjectAnswer]


def _repair_marker_answer(index: SubjectIndex, question: DeferredQuestion) -> SubjectAnswer:
    if not question.dedupe_marker.startswith(_REPAIR_PREFIX):
        return NOT_APPLICABLE
    states = repair_marker_subject_states(question.dedupe_marker, index.halt_markers)
    return _resolved(states) if states else UNDETERMINABLE


def _parked_task_answer(index: SubjectIndex, question: DeferredQuestion) -> SubjectAnswer:
    state = index.parked_task_states.get(question.pk)
    return _resolved([state]) if state is not None else NOT_APPLICABLE


def _session_answer(index: SubjectIndex, question: DeferredQuestion) -> SubjectAnswer:
    state = index.session_states.get(_session_pk(question) or 0)
    return _resolved([state]) if state is not None else NOT_APPLICABLE


#: Consulted in order; the first APPLICABLE source owns the row (see the module docstring).
_SUBJECT_SOURCES: tuple[SubjectSource, ...] = (_repair_marker_answer, _parked_task_answer, _session_answer)


def _session_pk(question: DeferredQuestion) -> int | None:
    """The ``Session`` pk *question* names, or ``None`` for a harness UUID / blank."""
    return int(question.session_id) if question.session_id.isdigit() else None


def _session_pks(questions: Sequence[DeferredQuestion]) -> set[int]:
    return {pk for pk in (_session_pk(q) for q in questions) if pk is not None}


def _parked_ticket_states(question_pks: set[int]) -> dict[int, str]:
    if not question_pks:
        return {}
    return dict(
        DeferredQuestion.objects.filter(pk__in=question_pks, parked_task__isnull=False).values_list(
            "pk", "parked_task__ticket__state"
        )
    )


def _session_ticket_states(session_pks: set[int]) -> dict[int, str]:
    if not session_pks:
        return {}
    return dict(Session.objects.filter(pk__in=session_pks).values_list("pk", "ticket__state"))
