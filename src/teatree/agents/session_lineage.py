"""Which earlier session a headless task continues, and which session its honesty escalation answers to."""

from teatree.core.models import HonestyEscalation, Task
from teatree.core.models.honesty_escalation import EscalationSubject
from teatree.core.models.task import SERVER_SESSION_ID_RE


def continuation_source(task: Task) -> Task | None:
    """The task whose conversation *task* continues, read off its stored discriminator."""
    if task.session_continuation == Task.SessionContinuation.SELF:
        return task
    if task.session_continuation == Task.SessionContinuation.PARENT:
        return task.parent_task
    return None


def resumable_lineage(task: Task) -> list[Task]:
    """The chain of conversations *task* may continue, nearest first.

    SELF yields the row itself — a retry that reopened it in place continues its own last
    attempt, which its unchanged ``parent_task`` can never reach.
    """
    lineage: list[Task] = []
    seen: set[int] = set()
    current = continuation_source(task)
    while current is not None and current.pk is not None and int(current.pk) not in seen:
        seen.add(int(current.pk))
        lineage.append(current)
        current = continuation_source(current)
    return lineage


def resume_session_id(task: Task, *, harness: str = "") -> str:
    for current in resumable_lineage(task):
        if session_ids := _session_uuids(current, harness=harness):
            return session_ids[0]
    return ""


def honesty_subject(task: Task) -> EscalationSubject | None:
    return HonestyEscalation.first_active_subject(_lineage_subjects(task))


def _lineage_subjects(task: Task) -> list[EscalationSubject]:
    own = task.session.agent_id if task.session_id else ""  # ty: ignore[unresolved-attribute]
    subjects = [EscalationSubject(own, int(task.pk))]
    current = task.parent_task
    while current is not None:
        subjects.extend(EscalationSubject(session_id, int(current.pk)) for session_id in _session_uuids(current))
        current = current.parent_task
    return subjects


def _session_uuids(task: Task, *, harness: str = "") -> list[str]:
    attempts = task.attempts.order_by("-pk")  # ty: ignore[unresolved-attribute]
    if harness:
        attempts = attempts.filter(selected_harness__in=(harness, "") if harness == "claude_sdk" else (harness,))
    last_attempt = attempts.first()
    attempt_session = last_attempt.agent_session_id if last_attempt else ""
    # ``Session.agent_id`` predates the open harness registry and belongs to the
    # Claude SDK lineage. New harnesses record their typed session exclusively on
    # TaskAttempt; forwarding this legacy UUID would resume the wrong provider.
    agent_id = task.session.agent_id if task.session_id and harness in {"", "claude_sdk"} else ""  # ty: ignore[unresolved-attribute]
    return [session_id for session_id in (attempt_session, agent_id) if SERVER_SESSION_ID_RE.match(session_id)]
