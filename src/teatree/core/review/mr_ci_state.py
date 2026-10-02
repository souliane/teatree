"""A merge request's :class:`~teatree.core.review.mr_triage.CiState`, read off the LIST payload.

Everything here reads the payload a forge already returned, so nothing fetches:
a caller holding the row holds the answer. The vocabulary is
:class:`CiState` — the triage ladder's four states — so the batch gate, the
triage scanner and the open-merge-request scanner cannot disagree about what
"green" means.

:data:`CI_BY_STATUS` is deliberately an ALLOWLIST rather than a
green-or-else split. A cancelled, skipped or manual pipeline, and any status
nobody has seen, resolve to ``UNKNOWN`` — never satisfying a green and never
naming a failure the author could act on — because a status outside the table
is a status this code does not understand, and guessing which side of the line
it falls on is how a not-passed pipeline gets read as passed.
"""

from typing import cast

from teatree.core.review.mr_triage import CiState
from teatree.types import RawAPIDict

#: The keys any forge populates with a pipeline/CI status. A payload carrying
#: NONE of them was never enriched — its CI is unreadable, not red.
PIPELINE_FIELDS = ("head_pipeline", "status_check_rollup", "mergeable_state")

#: A pipeline is green only when it explicitly succeeded.
GREEN_STATUSES = frozenset({"success", "succeeded", "passed"})

#: The forge's pipeline vocabulary, reduced to the states the ladder reads.
CI_BY_STATUS: dict[str, CiState] = {
    **dict.fromkeys(GREEN_STATUSES, CiState.GREEN),
    "running": CiState.PENDING,
    "pending": CiState.PENDING,
    "created": CiState.PENDING,
    "preparing": CiState.PENDING,
    "scheduled": CiState.PENDING,
    "waiting_for_resource": CiState.PENDING,
    "failed": CiState.FAILED,
    "failure": CiState.FAILED,
    "error": CiState.FAILED,
}


def carries_pipeline_field(pr: RawAPIDict) -> bool:
    return any(name in pr for name in PIPELINE_FIELDS)


def pipeline_status(pr: RawAPIDict) -> str:
    """The most relevant pipeline state across host shapes; ``""`` when absent.

    GitLab merge requests expose ``head_pipeline.status``; GitHub pull requests
    expose a nested ``status_check_rollup`` or ``mergeable_state``.
    """
    pipeline = pr.get("head_pipeline")
    if isinstance(pipeline, dict):
        status = cast("RawAPIDict", pipeline).get("status")
        if isinstance(status, str):
            return status
    rollup = pr.get("status_check_rollup")
    if isinstance(rollup, dict):
        state = cast("RawAPIDict", rollup).get("state")
        if isinstance(state, str):
            return state
    state = pr.get("mergeable_state")
    return state if isinstance(state, str) else ""


def ci_state_from_status(status: str) -> CiState:
    return CI_BY_STATUS.get(status.casefold(), CiState.UNKNOWN)


def ci_state(pr: RawAPIDict) -> CiState:
    return ci_state_from_status(pipeline_status(pr))
