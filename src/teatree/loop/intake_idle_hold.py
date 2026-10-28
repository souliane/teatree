"""Why issue intake claims nothing new while a started ticket sits idle (#5187).

A stalled ticket releases its claim and frees a budget slot, so intake would keep adding work
while that ticket waits for nobody. The hold reads the recovery sweep's own idle rules, minus
the tickets waiting on a pending question — the one reason an idle ticket is owed nothing.
"""

from datetime import datetime

from teatree.core.factory.stuck_ticket_halt import ticket_halt_is_current
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_subjects import SubjectIndex
from teatree.loop.stuck_ticket_redispatch import idle_started_author_tickets


def idle_hold_reason(hold_hours: int, *, now: datetime) -> str | None:
    """The sentence naming every ticket that holds intake, or ``None`` when none does.

    A window of zero or less is off, so an unset setting costs no query.
    """
    if hold_hours <= 0:
        return None
    idle = idle_started_author_tickets(now=now, threshold_hours=hold_hours)
    if not idle:
        return None
    questions = list(DeferredQuestion.pending())
    index = SubjectIndex.build(questions)
    asked_about = {pk for question in questions for pk in index.ticket_ids_for(question) or ()}
    holders = [ticket for ticket in idle if ticket.pk not in asked_about and not ticket_halt_is_current(ticket)]
    if not holders:
        return None
    named = ", ".join(f"ticket {ticket.pk} ({ticket.issue_url or 'no issue'})" for ticket in holders)
    return f"{named} idle {hold_hours}h+ with no active task, live PR or pending question"
