"""The one payload→:class:`CiState` reading every review surface shares.

The table is an allowlist, so the load-bearing cases are the ones OUTSIDE it: a
cancelled pipeline and an unenriched payload must both answer UNKNOWN rather
than pick a side, because a status this code does not understand is exactly the
one it must not guess about.
"""

import pytest

from teatree.core.review.mr_ci_state import carries_pipeline_field, ci_state, ci_state_from_status, pipeline_status
from teatree.core.review.mr_triage import CiState
from teatree.types import RawAPIDict


def test_carries_pipeline_field_detects_each_forge_shape() -> None:
    assert carries_pipeline_field({"head_pipeline": {"status": "failed"}})
    assert carries_pipeline_field({"status_check_rollup": {"state": "failure"}})
    assert carries_pipeline_field({"mergeable_state": "blocked"})
    assert not carries_pipeline_field({"iid": 1, "title": "no ci"})


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"head_pipeline": {"status": "running"}}, "running"),
        ({"status_check_rollup": {"state": "failure"}}, "failure"),
        ({"mergeable_state": "blocked"}, "blocked"),
        ({"head_pipeline": None, "mergeable_state": "clean"}, "clean"),
        ({"iid": 1}, ""),
    ],
)
def test_pipeline_status_reads_whichever_shape_the_forge_populated(payload: RawAPIDict, expected: str) -> None:
    assert pipeline_status(payload) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("success", CiState.GREEN),
        ("SUCCESS", CiState.GREEN),
        ("passed", CiState.GREEN),
        ("running", CiState.PENDING),
        ("waiting_for_resource", CiState.PENDING),
        ("failed", CiState.FAILED),
        ("error", CiState.FAILED),
        ("canceled", CiState.UNKNOWN),
        ("skipped", CiState.UNKNOWN),
        ("manual", CiState.UNKNOWN),
        ("", CiState.UNKNOWN),
    ],
)
def test_a_status_outside_the_allowlist_is_unknown_not_a_guess(status: str, expected: CiState) -> None:
    assert ci_state_from_status(status) == expected


def test_an_unenriched_payload_is_unknown_rather_than_failed() -> None:
    assert ci_state({"iid": 1, "title": "never enriched"}) == CiState.UNKNOWN
    assert ci_state({"head_pipeline": {"status": "success"}}) == CiState.GREEN
