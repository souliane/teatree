"""The admission score shared by both headless chokepoints, loop claims and task-list inspection.

One score per waiting task, the sum of named parts, highest first; the older row wins a tie,
so age is the last part. Every caller orders through :data:`ADMISSION_ORDER`, so a new part
is added here without touching any of them.
"""

from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.expressions import BaseExpression

from teatree.core.modelkit.phases import cheap_phase_spellings, phase_spellings

ADMISSION_SCORE_ALIAS = "_admission_score"
ADMISSION_ORDER: tuple[str, ...] = (f"-{ADMISSION_SCORE_ALIAS}", "pk")

#: Stage part: a review-lane row outranks every other row, continuing work outranks a new ticket.
_REVIEW_STAGE = 400
_CONTINUING_STAGE = 100
_NEW_TICKET_STAGE = 0
#: Priority part: an expedited ticket lifts its work above all unexpedited non-review work, never above a review.
_EXPEDITED_PRIORITY = 200


def _new_ticket_autostart_q() -> Q:
    """A parentless initial phase on a ticket still before its first recorded plan."""
    from teatree.core.models import Ticket  # noqa: PLC0415 — FSM state is the fact the rank needs

    autostart_phases = phase_spellings("planning") + phase_spellings("scoping")
    initial_states = (Ticket.State.NOT_STARTED, Ticket.State.SCOPED, Ticket.State.WORK_STARTED)
    return Q(parent_task__isnull=True) & Q(phase__in=autostart_phases) & Q(ticket__state__in=initial_states)


def _priority_part() -> Case:
    return Case(
        When(ticket__expedited=True, then=Value(_EXPEDITED_PRIORITY)),
        default=Value(0),
        output_field=IntegerField(),
    )


def _stage_part() -> Case:
    return Case(
        When(phase__in=cheap_phase_spellings(), then=Value(_REVIEW_STAGE)),
        When(_new_ticket_autostart_q(), then=Value(_NEW_TICKET_STAGE)),
        default=Value(_CONTINUING_STAGE),
        output_field=IntegerField(),
    )


def admission_priority_annotations() -> dict[str, BaseExpression]:
    return {ADMISSION_SCORE_ALIAS: _priority_part() + _stage_part()}


__all__ = ["ADMISSION_ORDER", "ADMISSION_SCORE_ALIAS", "admission_priority_annotations"]
