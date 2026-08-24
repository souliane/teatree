"""The agent-session index — the transcript viewer's home in the nav (#3873).

``/dash/transcript/<session_id>/`` has always answered 200, but the only link to it
was inside a ticket's drawer, so reading a transcript required already knowing which
ticket to open. This is the list that answers "which sessions ran, and what did they
do", one row per agent session, each linking to its redacted tail.

Nothing here renders free text an agent produced — a row carries the dispatch facts
(ticket, phase, model, lane, outcome) and never the attempt's ``error`` body. The
transcript click-through is the one surface that shows agent output, and it routes
every line through the shared leak-gate redactor first.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import F, Window
from django.db.models.functions import RowNumber

from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.selectors._helpers import _humanize_duration
from teatree.dash.skills import skill_bundle

#: Sessions listed per page. The attempt table grows without bound (340k rows on the
#: deployed box), so the index is a recent window, not a full history.
SESSION_ROWS = 50


@dataclass(frozen=True, slots=True)
class SessionRow:
    """One agent session, identified by the transcript it wrote."""

    agent_session_id: str
    ticket_id: int | None
    ticket_number: str
    short_description: str
    phase: str
    model: str
    lane: str
    outcome: str
    started_at: datetime
    duration: str
    #: The resolved bundle the dispatch RAN with, and whether an empty one is a
    #: fault (#3886) — a session whose bundle resolved to nothing behaves nothing
    #: like the phase intended, and looks identical here without this.
    skills: tuple[str, ...] = ()
    skills_fault: bool = False


def build_session_index() -> tuple[SessionRow, ...]:
    """The most recent :data:`SESSION_ROWS` agent sessions, newest first.

    One row per session rather than per attempt: a session that produced several
    attempts writes ONE transcript, so the newest attempt of each is the row that
    describes it. The de-duplication is a SQL window so the query stays bounded by
    the page size instead of by how many attempts share a session.
    """
    latest_of_each = (
        TaskAttempt.objects.exclude(agent_session_id="")
        .alias(rank=Window(RowNumber(), partition_by="agent_session_id", order_by=F("pk").desc()))
        .filter(rank=1)
        .select_related("task", "task__ticket")
        .order_by("-pk")[:SESSION_ROWS]
    )
    return tuple(_row(attempt) for attempt in latest_of_each)


def _row(attempt: TaskAttempt) -> SessionRow:
    ticket = attempt.task.ticket
    skills, skills_fault = skill_bundle(attempt)
    return SessionRow(
        agent_session_id=attempt.agent_session_id,
        ticket_id=ticket.pk if ticket else None,
        ticket_number=ticket.ticket_number if ticket else "",
        short_description=ticket.short_description if ticket else "",
        phase=attempt.task.phase,
        model=attempt.model,
        lane=attempt.lane,
        outcome=attempt.get_outcome_display() if attempt.outcome else "",  # ty: ignore[unresolved-attribute]
        started_at=attempt.started_at,
        duration=_duration(attempt),
        skills=skills,
        skills_fault=skills_fault,
    )


def _duration(attempt: TaskAttempt) -> str:
    if attempt.ended_at is None:
        return ""
    return _humanize_duration((attempt.ended_at - attempt.started_at).total_seconds())
