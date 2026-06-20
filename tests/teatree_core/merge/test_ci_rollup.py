"""GitHub required-checks rollup classification (BLUEPRINT §17.4.3 step 3).

``fetch_required_checks_status`` re-verifies the live required-checks at merge
time. GitHub branch protection keys the *newest* check-run per context name, so
a cancelled/stale run that left a spurious FAILURE check-run on the same head
commit must NOT block a merge that a newer SUCCESS for the same name supersedes.

These tests pin that dedupe-newest-per-name semantics: the verdict is computed
over the newest check-run per identity, matching ``mergeStateStatus=CLEAN`` the
forge itself computes. Only the unstoppable external — the ``gh`` subprocess —
is stubbed; the classification under test is real.
"""

import json
from collections.abc import Callable
from unittest.mock import patch

from teatree.core.merge import fetch_required_checks_status
from teatree.core.merge.ci_rollup import _dedupe_newest_per_name

_SLUG = "souliane/teatree"
_PR_ID = 2580


def _gh_stub(rollup: list[dict[str, object]]) -> Callable[[list[str]], tuple[int, str, str]]:
    """A ``gh`` runner that returns *rollup* for the statusCheckRollup query."""

    def run(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "statusCheckRollup" in joined:
            return (0, json.dumps(rollup), "")
        return (0, "", "")

    return run


def _verdict(rollup: list[dict[str, object]]) -> str:
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub(rollup)):
        return fetch_required_checks_status(_SLUG, _PR_ID, host_kind="github")


def _check_run(
    name: str,
    *,
    status: str = "COMPLETED",
    conclusion: str = "SUCCESS",
    started_at: str,
    completed_at: str,
) -> dict[str, object]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": started_at,
        "completedAt": completed_at,
    }


def _status_context(name: str, *, state: str, created_at: str) -> dict[str, object]:
    return {
        "__typename": "StatusContext",
        "context": name,
        "state": state,
        "createdAt": created_at,
    }


class TestDedupeNewestPerName:
    def test_stale_failure_superseded_by_newer_success_is_green(self) -> None:
        # The PR #2580 incident: a cancelled stale-merge-ref run left
        # sbom=FAILURE on the same head, while the authoritative newer run
        # has sbom=SUCCESS. GitHub branch protection keys the newest per
        # name, so mergeStateStatus is CLEAN — the verdict must be green.
        rollup = [
            _check_run(
                "sbom",
                conclusion="FAILURE",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
        ]
        assert _verdict(rollup) == "green"

    def test_newest_entry_genuinely_failing_is_failed(self) -> None:
        # An older SUCCESS superseded by a newer genuine FAILURE for the same
        # name must still fail — newest wins in both directions.
        rollup = [
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
            _check_run(
                "sbom",
                conclusion="FAILURE",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
        ]
        assert _verdict(rollup) == "failed"

    def test_newest_entry_pending_is_pending(self) -> None:
        # An older SUCCESS superseded by a newer still-running run for the same
        # name is pending — the head is not yet conclusively green.
        rollup = [
            _check_run(
                "build",
                conclusion="SUCCESS",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
            _check_run(
                "build",
                status="IN_PROGRESS",
                conclusion="",
                started_at="2026-06-19T11:00:00Z",
                completed_at="",
            ),
        ]
        assert _verdict(rollup) == "pending"

    def test_mixed_checkrun_and_status_context_dedupe_each_namespace(self) -> None:
        # A CheckRun named "lint" (stale FAILURE → newer SUCCESS) and a
        # StatusContext named "coverage" (stale failure → newer success) both
        # dedupe within their own namespace; the verdict is green.
        rollup = [
            _check_run(
                "lint",
                conclusion="FAILURE",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
            _check_run(
                "lint",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
            _status_context("coverage", state="FAILURE", created_at="2026-06-19T10:00:00Z"),
            _status_context("coverage", state="SUCCESS", created_at="2026-06-19T11:00:00Z"),
        ]
        assert _verdict(rollup) == "green"

    def test_status_context_stale_failure_superseded_by_newer_success_is_green(self) -> None:
        rollup = [
            _status_context("legacy-ci", state="FAILURE", created_at="2026-06-19T10:00:00Z"),
            _status_context("legacy-ci", state="SUCCESS", created_at="2026-06-19T11:00:00Z"),
        ]
        assert _verdict(rollup) == "green"

    def test_distinct_names_each_failing_still_failed(self) -> None:
        # Two genuinely distinct failing checks (different names) must both
        # count — dedupe must not collapse distinct names into one.
        rollup = [
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
            _check_run(
                "tests",
                conclusion="FAILURE",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
        ]
        assert _verdict(rollup) == "failed"

    def test_checkrun_and_status_context_same_name_are_distinct_identities(self) -> None:
        # A CheckRun named "x" (SUCCESS) and a StatusContext named "x"
        # (FAILURE) are different identities (different __typename), so the
        # FAILURE is NOT superseded by the CheckRun — verdict is failed.
        rollup = [
            _check_run(
                "x",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
            _status_context("x", state="FAILURE", created_at="2026-06-19T10:00:00Z"),
        ]
        assert _verdict(rollup) == "failed"

    def test_dedupe_is_order_independent_newest_success_first(self) -> None:
        # Same incident as the first test but the newer SUCCESS is listed
        # BEFORE the stale FAILURE — the incumbent (newer) must not be
        # clobbered by an older entry encountered later. Verdict stays green.
        rollup = [
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
            _check_run(
                "sbom",
                conclusion="FAILURE",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
        ]
        assert _verdict(rollup) == "green"


class TestDedupeHelperEdgeCases:
    """Defensive branches of ``_dedupe_newest_per_name`` reached directly.

    The backend pre-filters non-dicts out of the rollup, so these shapes do
    not reach the helper through the public ``fetch_required_checks_status``
    path — but the helper is defensive in its own right, so its guards are
    exercised here.
    """

    def test_non_dict_entry_is_skipped(self) -> None:
        rollup = [
            "not-a-dict",
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
        ]
        deduped = _dedupe_newest_per_name(rollup)
        assert deduped == [
            {
                "__typename": "CheckRun",
                "name": "sbom",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-06-19T11:00:00Z",
                "completedAt": "2026-06-19T11:05:00Z",
            },
        ]

    def test_entry_with_no_identity_is_kept_unkeyed(self) -> None:
        # An entry with neither name nor context cannot be deduped — it is
        # kept verbatim so the per-entry classifier still sees it (fail-closed).
        nameless = {"__typename": "CheckRun", "conclusion": "FAILURE", "status": "COMPLETED"}
        keyed = _check_run(
            "sbom",
            conclusion="SUCCESS",
            started_at="2026-06-19T11:00:00Z",
            completed_at="2026-06-19T11:05:00Z",
        )
        deduped = _dedupe_newest_per_name([keyed, nameless])
        assert nameless in deduped
        assert keyed in deduped
        assert len(deduped) == 2
