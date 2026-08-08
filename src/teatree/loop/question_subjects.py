"""Which ticket(s) a pending ``DeferredQuestion`` is about (#4178).

The repair-loop reconcile (:mod:`teatree.loop.repair_halt_reconcile`) could only
answer that for a row carrying a ``repair-`` ``dedupe_marker`` — 6 of 70 pending
rows when #4178 was measured. The other 64 had no marker to key a subject off, so
no resolver could reach them and each waited on a human.

This is the generalised answer. Three sources, tried in order of how directly they
name the subject:

1. ``parked_task`` — an explicit FK to the ``Task`` whose park raised the question,
    so its ticket IS the subject. The headless needs-input lane sets it.
2. the repair markers — unchanged, delegated verbatim to
    :func:`~teatree.loop.repair_halt_reconcile.repair_marker_subject_states`.
3. ``session_id`` when it is a ``Session`` pk. The column holds EITHER a harness
    session UUID (the away-mode ``AskUserQuestion`` hook) or ``str(task.session_id)``
    from the task-derived producers. Only an all-digit value that resolves to a real
    ``Session`` row is accepted, so a harness UUID never derives a subject.

The marker parse is deliberately NOT widened past the ``repair-`` prefixes: a
non-repair marker's second field is not a ticket pk (``attachment-hold:5``), and
reading it as one would drain a live owner question on a coincidence.

Every source answers ``None`` — undeterminable — rather than guessing, and the
sweep treats ``None`` as KEEP.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from teatree.core.models import Session
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.repair_halt_reconcile import halt_marker_subject_states, repair_marker_subject_states

_HALT_PREFIX = "repair-halt:"


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
        parked = self.parked_task_states.get(question.pk)
        if parked is not None:
            return [parked]
        from_marker = repair_marker_subject_states(question.dedupe_marker, self.halt_markers)
        if from_marker:
            return from_marker
        session = self.session_states.get(_session_pk(question) or 0)
        return [session] if session is not None else None


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
