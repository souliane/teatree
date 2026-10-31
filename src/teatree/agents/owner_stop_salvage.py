"""A refused owner stop never discards the finished work behind it (#5096).

When the stop is refused but the run committed its work, the recorder completes the task without the stop and
parks the refused question as an internal row, withheld for the gate's reasons, never posted as written.
"""

from teatree.agents.envelope_refusal import is_owner_stop_refusal
from teatree.agents.result_schema import AgentResultBlob
from teatree.agents.result_schema_owner_stop import owner_stop_refusal_reason
from teatree.core.models import DeferredQuestion, Task
from teatree.core.models.question_text import question_fingerprint
from teatree.core.models.question_withheld import WITHHELD_ACTION
from teatree.core.question_heal import record_withheld

_OWNER_STOP_KEYS = frozenset(
    {"needs_user_input", "user_input_reason", "user_input_kind", "user_input_checked", "user_input_card"}
)
_DEFAULT_QUESTION = "Agent needs human input"


def salvageable_refusal(error: str, result: AgentResultBlob) -> bool:
    return is_owner_stop_refusal(error) and bool(result.get("files_modified"))


def without_owner_stop(result: AgentResultBlob) -> AgentResultBlob:
    return {key: value for key, value in result.items() if key not in _OWNER_STOP_KEYS}


def withhold_refused_stop(task: Task, stop: AgentResultBlob, refusal: str) -> DeferredQuestion:
    question = str(stop.get("user_input_reason") or "").strip() or _DEFAULT_QUESTION
    return record_withheld(
        question,
        [owner_stop_refusal_reason(refusal)],
        parked_task=task,
        task_session=task.session,
        dedupe_marker=f"needs-input:{task.ticket_id}:{question_fingerprint(question)}",
    )


def withheld_stop_lines(task: Task) -> list[str]:
    """The line a phase queued behind a withheld stop starts with: what was withheld, why, and the way to re-ask."""
    if task.parent_task_id is None:
        return []
    rows = DeferredQuestion.internal_pending().filter(
        parked_task_id=task.parent_task_id, audits__action=WITHHELD_ACTION
    )
    return [
        f"Your earlier stop for the owner was withheld ({note}): {row.question} "
        "If only the owner can decide it, ask it again in plain words with `t3 <overlay> questions record`; "
        "otherwise decide it yourself."
        for row in rows
        for note in row.audits.filter(action=WITHHELD_ACTION).values_list("note", flat=True)[:1]
    ]
