"""The admission rank shared by both headless chokepoints, loop claims and task-list inspection."""

from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.expressions import BaseExpression

from teatree.core.modelkit.phases import cheap_phase_spellings, phase_spellings

ADMISSION_RANK_ALIAS = "_admission_rank"
ADMISSION_ORDER: tuple[str, ...] = (ADMISSION_RANK_ALIAS, "pk")


def _new_ticket_autostart_q() -> Q:
    """A parentless initial phase on a ticket still before its first recorded plan."""
    from teatree.core.models import Ticket  # noqa: PLC0415 — FSM state is the fact the rank needs

    autostart_phases = phase_spellings("planning") + phase_spellings("scoping")
    initial_states = (Ticket.State.NOT_STARTED, Ticket.State.SCOPED, Ticket.State.WORK_STARTED)
    return Q(parent_task__isnull=True) & Q(phase__in=autostart_phases) & Q(ticket__state__in=initial_states)


def admission_priority_annotations() -> dict[str, BaseExpression]:
    """SQL rank: expedited review 0; review 1; expedited work 2; continuing work 3; new-ticket first phase 4."""
    review = Q(phase__in=cheap_phase_spellings())
    expedited = Q(ticket__expedited=True)
    return {
        ADMISSION_RANK_ALIAS: Case(
            When(review & expedited, then=Value(0)),
            When(review, then=Value(1)),
            When(expedited, then=Value(2)),
            When(_new_ticket_autostart_q(), then=Value(4)),
            default=Value(3),
            output_field=IntegerField(),
        )
    }


__all__ = ["ADMISSION_ORDER", "ADMISSION_RANK_ALIAS", "admission_priority_annotations"]
