"""Resolve the owning ticket source of a deferred owner question."""

import re
from typing import Literal

from teatree.core.models import DeferredQuestion, Session, Task, Ticket

QuestionSource = Literal["repair", "stuck", "parked", "session"]
STUCK_HALT_PK_RE = re.compile(r"^stuck-redispatch-halt:(\d+)$")
_REPAIR_KEYED = re.compile(r"^repair-(?:stall|cap):(\d+):")


def question_source(question: DeferredQuestion) -> tuple[QuestionSource, int | None] | None:
    """Return the first applicable source and its identifier, if the marker has one."""
    marker = question.dedupe_marker
    if marker.startswith("repair-"):
        match = _REPAIR_KEYED.match(marker)
        return "repair", int(match.group(1)) if match is not None else None
    if match := STUCK_HALT_PK_RE.match(marker):
        return "stuck", int(match.group(1))
    if question.parked_task_id is not None:
        return "parked", question.parked_task_id
    if question.task_session_id is not None:
        return "session", question.task_session_id
    return None


def single_question_ticket_id(question: DeferredQuestion) -> int | None:
    """Return a live ticket only when one source can identify it."""
    source = question_source(question)
    if source is None:
        return None
    kind, identifier = source
    if kind in {"repair", "stuck"}:
        candidate = identifier
    elif kind == "parked":
        candidate = Task.objects.filter(pk=identifier).values_list("ticket_id", flat=True).first()
    else:
        candidate = Session.objects.filter(pk=identifier).values_list("ticket_id", flat=True).first()
    return candidate if candidate is not None and Ticket.objects.filter(pk=candidate).exists() else None
