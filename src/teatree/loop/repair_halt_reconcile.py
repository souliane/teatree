"""Auto-drain stale repair-loop escalation questions when their subject reconciles (#3692).

A repair-loop escalation records a durable ``DeferredQuestion`` when an auto-retry
is halted (``repair-halt:``), a phase stalls on two identical failures
(``repair-stall:``), or a phase exhausts its iteration budget (``repair-cap:``).
Each asks the owner "how should it proceed — investigate, rework, or ignore?".

When the escalation's SUBJECT — the ticket(s) whose stall raised it — subsequently
reaches a terminal state (its PR merged, the ticket delivered, or it was ignored),
the question is MOOT: the loop will never retry that phase again
(:func:`~teatree.loop.transient_requeue._non_terminal_failed_tasks` excludes
terminal tickets), so the only possible answer is "ignore". Left pending, these
moot rows pile up in the away-mode queue and bury the one live question the owner
actually needs to answer.

This tick reconcile dismisses exactly the provably-moot rows and no others. The
conservatism guard is per-subject: a row is drained ONLY when EVERY ticket that
raised it is terminal. A ticket-keyed marker (``repair-stall``/``repair-cap``)
carries its subject ticket pk directly; a fingerprint-keyed ``repair-halt`` marker
collapses several tickets onto one row, so its subjects are re-derived from the
parked (:data:`~teatree.loop.transient_requeue.HALT_STAMP`) tasks that share the
marker. If even one such ticket is still live — or the subject cannot be
determined at all — the row is KEPT. Dropping a question whose subject is still
live is the failure mode this must never commit.

Lives in ``teatree.loop`` (orchestration): it composes the ``DeferredQuestion``
and ``Task``/``Ticket`` domain models with the ``transient_requeue`` escalation
marker, so only an orchestration-layer module may own it.
"""

from collections import defaultdict

from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.transient_requeue import HALT_STAMP, escalation_marker

_HALT_PREFIX = "repair-halt:"
_TICKET_KEYED_PREFIXES = ("repair-stall:", "repair-cap:")
#: A ticket-keyed marker is ``<kind>:<ticket-pk>:<phase>`` — three colon-separated fields.
_TICKET_KEYED_FIELDS = 3


def resolve_reconciled_repair_halts() -> int:
    """Dismiss pending repair-loop questions whose every subject ticket is terminal.

    Returns the number of questions drained. A question is dismissed as STALE only
    when its subjects are determinable AND every one is in a terminal state; any
    live or undeterminable subject leaves the row pending untouched.
    """
    pending = list(
        DeferredQuestion.objects.filter(
            answered_at__isnull=True,
            dismissed_at__isnull=True,
            dedupe_marker__startswith="repair-",
        )
    )
    if not pending:
        return 0
    halt_subjects = _halt_marker_subject_states(
        {q.dedupe_marker for q in pending if q.dedupe_marker.startswith(_HALT_PREFIX)}
    )
    resolved = 0
    for question in pending:
        states = _subject_states(question.dedupe_marker, halt_subjects)
        if states and all(state in Ticket._TERMINAL_STATES for state in states):  # noqa: SLF001 — model SSOT terminal set
            reason = f"repair-loop escalation moot: every subject ticket is terminal [{question.dedupe_marker}]"
            question.mark_stale(reason)
            resolved += 1
    return resolved


def _subject_states(marker: str, halt_subject_states: dict[str, list[str]]) -> list[str] | None:
    """The FSM state of every ticket that raised *marker*, or ``None`` if undeterminable.

    A ticket-keyed ``repair-stall``/``repair-cap`` marker carries its subject pk; a
    fingerprint-keyed ``repair-halt`` marker's subjects come from the pre-computed
    parked-task map. ``None`` (undeterminable) is treated as "keep" by the caller —
    the conservative default that never drops a question on a guess.
    """
    if marker.startswith(_TICKET_KEYED_PREFIXES):
        return _ticket_keyed_states(marker)
    if marker.startswith(_HALT_PREFIX):
        return halt_subject_states.get(marker)
    return None


def _ticket_keyed_states(marker: str) -> list[str] | None:
    """The single subject ticket's state for a ``repair-stall:<pk>:<phase>`` marker, else ``None``."""
    parts = marker.split(":", _TICKET_KEYED_FIELDS - 1)
    if len(parts) < _TICKET_KEYED_FIELDS or not parts[1].isdigit():
        return None
    state = Ticket.objects.filter(pk=int(parts[1])).values_list("state", flat=True).first()
    return [state] if state is not None else None


def _halt_marker_subject_states(markers: set[str]) -> dict[str, list[str]]:
    """Map each ``repair-halt`` marker in *markers* to the states of the tickets that raised it.

    One pass over the parked (:data:`HALT_STAMP`) FAILED tasks — the durable record
    of which tickets a fingerprint-keyed marker collapsed — re-deriving each task's
    :func:`escalation_marker` and grouping ticket states by it. A marker with no
    surviving parked task is absent from the map, so the caller keeps its question
    (the subject cannot be proven terminal).
    """
    if not markers:
        return {}
    by_marker: dict[str, list[str]] = defaultdict(list)
    parked = (
        Task.objects.filter(status=Task.Status.FAILED, execution_reason__contains=HALT_STAMP)
        .select_related("ticket")
        .prefetch_related("attempts")
    )
    for task in parked:
        marker = escalation_marker(task)
        if marker in markers:
            by_marker[marker].append(task.ticket.state)
    return dict(by_marker)
