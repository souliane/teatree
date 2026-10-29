from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.utils import timezone

from teatree.core.modelkit.db_retry import retry_on_locked
from teatree.core.modelkit.task_failure_taxonomy import AGENT_ABANDONED_PREFIX, FailureKind
from teatree.core.models.errors import LeaseLostError
from teatree.core.models.task_claim import _refuse_lost_lease
from teatree.core.models.types import JSONObject
from teatree.core.repair_loop import terminal_reason_fingerprint

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.task_attempt import TaskAttempt


@dataclass(frozen=True, slots=True)
class LateAttemptFields:
    exit_code: int
    error: str
    result: JSONObject


@dataclass(frozen=True, slots=True)
class AttemptInput:
    artifact_path: str
    exit_code: int
    error: str
    result: JSONObject | None
    usage_unknown: bool


def _late_fields(
    task: "Task", *, loss: LeaseLostError, exit_code: int, error: str, result: JSONObject
) -> LateAttemptFields:
    current = retry_on_locked(
        lambda: type(task).objects.filter(pk=task.pk).values("status", "failure_kind", "failure_reason").first()
    )
    if current is not None and current["status"] == task.Status.COMPLETED:
        return LateAttemptFields(0, "", {**result, "summary": "Run interrupted: task row already completed"})
    if current is not None and current["failure_kind"] == FailureKind.CANCELLED:
        return LateAttemptFields(
            exit_code, error, {**result, "summary": f"Run interrupted: {current['failure_reason']}"}
        )
    note = f"stuck_loop: {loss}"
    if error:
        note = f"{note}; run error: {error}"
    return LateAttemptFields(exit_code, note, result)


def complete_with_attempt(task: "Task", details: AttemptInput) -> "TaskAttempt":
    task_attempt_model = cast("type[TaskAttempt]", apps.get_model("core", "TaskAttempt"))
    artifact_path = details.artifact_path
    exit_code = details.exit_code
    error = details.error
    recorded_result = dict(details.result or {})
    lost: LeaseLostError | None = None
    try:
        retry_on_locked(lambda: _refuse_lost_lease(task))
    except LeaseLostError as exc:
        lost = exc
        fields = _late_fields(task, loss=exc, exit_code=exit_code, error=error, result=recorded_result)
        exit_code, error, recorded_result = fields.exit_code, fields.error, fields.result

    attempt = retry_on_locked(
        lambda: task_attempt_model.objects.create(
            task=task,
            ended_at=timezone.now(),
            exit_code=exit_code,
            artifact_path=artifact_path,
            error=error,
            result=recorded_result,
            usage_unknown=details.usage_unknown,
        )
    )
    if lost is not None:
        return attempt
    try:
        if exit_code == 0:
            retry_on_locked(lambda: task.complete(result_artifact_path=artifact_path))
        else:
            retry_on_locked(
                lambda: task.fail_claimed(
                    reason=error.strip() or f"{AGENT_ABANDONED_PREFIX}run exited {exit_code} with no error recorded"
                )
            )
    except LeaseLostError as exc:
        fields = _late_fields(task, loss=exc, exit_code=exit_code, error=error, result=recorded_result)
        attempt.exit_code, attempt.error, attempt.result = fields.exit_code, fields.error, fields.result
        attempt.error_fingerprint = terminal_reason_fingerprint(attempt.error)
        retry_on_locked(attempt.save)
    return attempt
