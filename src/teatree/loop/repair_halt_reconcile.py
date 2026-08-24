"""Derive the subject tickets behind a repair-loop escalation question (#3692).

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

This module answers only "which tickets raised it, and what state are they in?".
A ticket-keyed marker (``repair-stall``/``repair-cap``) carries its subject ticket
pk directly; a fingerprint-keyed ``repair-halt`` marker collapses several tickets
onto one row, so its subjects are re-derived from the parked
(:data:`~teatree.loop.transient_requeue.HALT_STAMP`) tasks that share the marker.
An undeterminable subject answers ``None``, which the sweep treats as KEEP —
dropping a question whose subject is still live is the failure mode this must
never commit.

Lives in ``teatree.loop`` (orchestration): it composes the ``DeferredQuestion``
and ``Task``/``Ticket`` domain models with the ``transient_requeue`` escalation
marker, so only an orchestration-layer module may own it. The sweep that consumes
this derivation is :mod:`teatree.loop.question_drain`, which applies the same
terminal-subject predicate to every pending row rather than only the ``repair-``
prefixed ones.
"""

from collections import defaultdict

from teatree.core.models import Task, Ticket
from teatree.loop.transient_requeue import HALT_STAMP, escalation_marker

_HALT_PREFIX = "repair-halt:"
_TICKET_KEYED_PREFIXES = ("repair-stall:", "repair-cap:")
#: A ticket-keyed marker is ``<kind>:<ticket-pk>:<phase>`` — three colon-separated fields.
_TICKET_KEYED_FIELDS = 3


def repair_marker_subject_tickets(markers: set[str]) -> dict[str, list[int]]:
    """Map each ``repair-`` marker in *markers* to the pks of the tickets that raised it.

    A ticket-keyed ``repair-stall``/``repair-cap`` marker carries its subject pk; a
    fingerprint-keyed ``repair-halt`` marker's subjects come from the parked tasks that
    share it. A marker absent from the result is undeterminable, which the caller treats
    as "keep" — the conservative default that never drops a question on a guess.

    Resolved for the whole sweep in one pass, and in ticket PKs rather than FSM states,
    so a caller can read anything the subject ticket carries — its state, or the pull
    requests recorded against it — without a per-row query.
    """
    resolved = halt_marker_subject_tickets({m for m in markers if m.startswith(_HALT_PREFIX)})
    keyed = {marker: pk for marker in markers if (pk := _ticket_keyed_pk(marker)) is not None}
    if keyed:
        live = set(Ticket.objects.filter(pk__in=set(keyed.values())).values_list("pk", flat=True))
        resolved.update({marker: [pk] for marker, pk in keyed.items() if pk in live})
    return resolved


def _ticket_keyed_pk(marker: str) -> int | None:
    """The subject ticket pk a ``repair-stall:<pk>:<phase>`` marker names, else ``None``."""
    if not marker.startswith(_TICKET_KEYED_PREFIXES):
        return None
    parts = marker.split(":", _TICKET_KEYED_FIELDS - 1)
    if len(parts) < _TICKET_KEYED_FIELDS or not parts[1].isdigit():
        return None
    return int(parts[1])


def halt_marker_subject_tickets(markers: set[str]) -> dict[str, list[int]]:
    """Map each ``repair-halt`` marker in *markers* to the pks of the tickets that raised it.

    One pass over the parked (:data:`HALT_STAMP`) FAILED tasks — the durable record
    of which tickets a fingerprint-keyed marker collapsed — re-deriving each task's
    :func:`escalation_marker` and grouping ticket pks by it. A marker with no
    surviving parked task is absent from the map, so the caller keeps its question
    (the subject cannot be proven terminal).
    """
    if not markers:
        return {}
    by_marker: dict[str, list[int]] = defaultdict(list)
    parked = Task.objects.filter(status=Task.Status.FAILED, execution_reason__contains=HALT_STAMP).prefetch_related(
        "attempts"
    )
    for task in parked:
        marker = escalation_marker(task)
        if marker in markers:
            by_marker[marker].append(task.ticket_id)
    return dict(by_marker)
