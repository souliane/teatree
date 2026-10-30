from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.utils import timezone

from teatree.core.modelkit.db_retry import retry_on_locked
from teatree.core.modelkit.task_failure_taxonomy import AGENT_ABANDONED_PREFIX, NOOP_OVER_COMPLETED_MARKER, FailureKind
from teatree.core.models.errors import LeaseLostError
from teatree.core.models.task_claim import _refuse_lost_lease, atomic_after_checkout_exit
from teatree.core.models.types import JSONObject

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
    strict_lease: bool = False


def _late_fields(
    task: "Task", *, loss: LeaseLostError, exit_code: int, error: str, result: JSONObject
) -> LateAttemptFields:
    current = retry_on_locked(
        lambda: type(task).objects.filter(pk=task.pk).values("status", "failure_kind", "failure_reason").first()
    )
    if current is not None and current["status"] == task.Status.COMPLETED:
        return LateAttemptFields(
            0, "", {**result, "summary": f"Run interrupted: {NOOP_OVER_COMPLETED_MARKER}task row already completed"}
        )
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
        if details.strict_lease:
            raise
        lost = exc
        fields = _late_fields(task, loss=exc, exit_code=exit_code, error=error, result=recorded_result)
        exit_code, error, recorded_result = fields.exit_code, fields.error, fields.result

    def create_attempt(code: int, reason: str, result: JSONObject) -> "TaskAttempt":
        return task_attempt_model.objects.create(
            # no-usage: core has no provider usage payload; post-turn crashes mark usage_unknown.
            task=task,
            ended_at=timezone.now(),
            exit_code=code,
            artifact_path=artifact_path,
            error=reason,
            result=result,
            usage_unknown=details.usage_unknown,
        )

    if lost is not None:
        return retry_on_locked(lambda: create_attempt(exit_code, error, recorded_result))

    def settle() -> "TaskAttempt":
        with atomic_after_checkout_exit(task):
            attempt = create_attempt(exit_code, error, recorded_result)
            if exit_code == 0:
                task.complete(result_artifact_path=artifact_path)
            else:
                task.fail_claimed(
                    reason=error.strip() or f"{AGENT_ABANDONED_PREFIX}run exited {exit_code} with no error recorded"
                )
            return attempt

    try:
        return retry_on_locked(settle)
    except LeaseLostError as exc:
        if details.strict_lease:
            raise
        fields = _late_fields(task, loss=exc, exit_code=exit_code, error=error, result=recorded_result)
        return retry_on_locked(lambda: create_attempt(fields.exit_code, fields.error, fields.result))


def complete_with_strict_lease(task: "Task", result: JSONObject) -> "TaskAttempt":
    return complete_with_attempt(
        task,
        AttemptInput(artifact_path="", exit_code=0, error="", result=result, usage_unknown=False, strict_lease=True),
    )
