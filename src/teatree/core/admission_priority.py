"""The ticket-state admission rank shared by loop claims and task-list inspection."""

from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.expressions import BaseExpression

from teatree.core.modelkit.phases import phase_spellings

ADMISSION_RANK_ALIAS = "_admission_rank"
ADMISSION_ORDER: tuple[str, ...] = (ADMISSION_RANK_ALIAS, "pk")


def _new_ticket_autostart_q() -> Q:
    """A parentless initial phase on a ticket still before its first recorded plan."""
    from teatree.core.models import Ticket  # noqa: PLC0415 — FSM state is the fact the rank needs

    autostart_phases = phase_spellings("planning") + phase_spellings("scoping")
    initial_states = (Ticket.State.NOT_STARTED, Ticket.State.SCOPED, Ticket.State.WORK_STARTED)
    return Q(parent_task__isnull=True) & Q(phase__in=autostart_phases) & Q(ticket__state__in=initial_states)


def admission_priority_annotations() -> dict[str, BaseExpression]:
    """SQL rank: continuing work 0; new-ticket first phase 1."""
    return {
        ADMISSION_RANK_ALIAS: Case(
            When(_new_ticket_autostart_q(), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
    }


__all__ = ["ADMISSION_ORDER", "ADMISSION_RANK_ALIAS", "admission_priority_annotations"]
