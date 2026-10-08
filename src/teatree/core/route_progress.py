"""Which candidates of a skill's ordered route already ran on a task and failed through their own fault."""

from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, TaskAttempt
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX

#: Kinds where the candidate ran and did not deliver. Outage and provisioning failures are
#: deliberately absent (no other candidate fixes them), as are the evidence and recording kinds,
#: which earn their one corrective retry on the same candidate.
CANDIDATE_FAULT_KINDS = frozenset({FailureKind.HARNESS_CRASH, FailureKind.LANDING_UNVERIFIED, FailureKind.RESULT_ERROR})


def failed_candidates(task: Task, source_skill: str) -> dict[int, int]:
    """Route candidate index -> its latest failed attempt's id, since the task's last usage-window park."""
    attempts = task.attempts.all()
    parks = attempts.filter(error__startswith=LIMIT_PARKED_PREFIX).order_by("-pk")
    last_park = parks.values_list("pk", flat=True).first()
    failed = attempts.filter(
        route_source_skill=source_skill,
        route_candidate_index__isnull=False,
        outcome__in=TaskAttempt.FAILED_OUTCOMES,
        failure_kind__in=CANDIDATE_FAULT_KINDS,
        pk__gt=last_park or 0,
    )
    return dict(failed.order_by("pk").values_list("route_candidate_index", "pk"))
