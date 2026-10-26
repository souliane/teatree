import re

from teatree.core.models import DeferredQuestion, Ticket

_STUCK_HALT = re.compile(r"^stuck-redispatch-halt:(\d+)$")
_REPAIR_KEYED = re.compile(r"^repair-(?:stall|cap):(\d+):")


def single_question_ticket_id(question: DeferredQuestion) -> int | None:
    marker = question.dedupe_marker
    if marker.startswith("repair-"):
        match = _REPAIR_KEYED.match(marker)
        candidate = int(match.group(1)) if match is not None else None
    elif match := _STUCK_HALT.match(marker):
        candidate = int(match.group(1))
    elif question.parked_task_id is not None:
        task = question.parked_task
        candidate = task.ticket_id if task is not None else None
    elif question.task_session_id is not None:
        session = question.task_session
        candidate = session.ticket_id if session is not None else None
    else:
        return None
    return candidate if candidate is not None and Ticket.objects.filter(pk=candidate).exists() else None
