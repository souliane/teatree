"""The admission score shared by both headless chokepoints, loop claims and task-list inspection.

One score per waiting task, the sum of named parts, highest first; a started pull request's
first admission breaks ties before the older task row. Every caller orders through
:data:`ADMISSION_ORDER`, so a new part is added here without touching any of them.
"""

from django.db.models import Case, Exists, F, IntegerField, OuterRef, Q, Value, When
from django.db.models.expressions import BaseExpression, OrderBy

from teatree.core.modelkit.phases import cheap_phase_spellings, phase_spellings
from teatree.core.models.pull_request import PullRequest, pull_request_start_expression

ADMISSION_SCORE_ALIAS = "_admission_score"
PR_STARTED_ALIAS = "_pr_started_at"
ADMISSION_ORDER: tuple[str | OrderBy, ...] = (
    f"-{ADMISSION_SCORE_ALIAS}",
    F(PR_STARTED_ALIAS).asc(nulls_last=True),
    "pk",
)

#: Stage part: a review-lane row outranks every other row, continuing work outranks a new ticket.
_REVIEW_STAGE = 400
_CONTINUING_STAGE = 100
_STARTED_PR_STAGE = 150
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
    """P, the ticket's priority — today ``Ticket.expedited``, the one source a label-driven priority replaces."""
    return Case(
        When(ticket__expedited=True, then=Value(_EXPEDITED_PRIORITY)),
        default=Value(0),
        output_field=IntegerField(),
    )


def _stage_part(started: Exists) -> Case:
    return Case(
        When(phase__in=cheap_phase_spellings(), then=Value(_REVIEW_STAGE)),
        When(started, then=Value(_STARTED_PR_STAGE)),
        When(_new_ticket_autostart_q(), then=Value(_NEW_TICKET_STAGE)),
        default=Value(_CONTINUING_STAGE),
        output_field=IntegerField(),
    )


def admission_priority_annotations() -> dict[str, BaseExpression]:
    started = Exists(PullRequest.objects.live().filter(ticket_id=OuterRef("ticket_id")))
    return {
        ADMISSION_SCORE_ALIAS: _priority_part() + _stage_part(started),
        PR_STARTED_ALIAS: Case(When(started, then=pull_request_start_expression())),
    }


__all__ = ["ADMISSION_ORDER", "ADMISSION_SCORE_ALIAS", "admission_priority_annotations"]
