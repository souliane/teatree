"""The daily cron runs the cheap backstops; the full shard lane and its durations refresh run weekly (#5006).

Each assertion evaluates ci.yml's real job conditions for one event, so a condition that
reads right but resolves wrong — a swapped branch, a cron literal no trigger declares —
fails here instead of on the next scheduled run.
"""

import re
from typing import Any

import pytest

from tests._actions_workflow import (
    CI_DAILY_CRON,
    CI_WEEKLY_CRON,
    WORKFLOWS,
    github_context,
    job_results,
    load,
    ran,
    triggers,
)

DAILY_JOBS = {"banned-terms-tree", "overlay-leak-tree", "term-source-drift", "uv-audit", "test-shuffle"}
MAINTENANCE_JOBS = {"refresh-durations", "scheduled-maintenance-report"}
FULL_SUITE = {"build-image", "test-shard", "jscpd-scan", "test"}
# Job keys behind the required contexts on main; `test` emits `test (3.13)`.
REQUIRED_CHECK_JOBS = {
    "lint",
    "test",
    "docs-drift",
    "uv-audit",
    "sbom",
    "blueprint-cross-pr",
    "banned-terms-tree",
    "overlay-leak-tree",
    "term-source-drift",
}


_PULL_REQUEST = github_context("pull_request", ref="refs/pull/42/merge")
_HEAVY = {"preflight": {"run_heavy_python": "true"}}
_DOCS_ONLY = {"preflight": {"run_heavy_python": "false"}}


def _ran(github: dict[str, Any], **kwargs: Any) -> set[str]:
    return ran(job_results(load(), github, **kwargs))


def _reporting_as_test(github: dict[str, Any], **kwargs: Any) -> list[str]:
    jobs = load()["jobs"]
    return sorted(key for key in _ran(github, **kwargs) if jobs[key].get("name", key) == "test")


class TestTheTwoCrons:
    def test_the_workflow_declares_the_daily_and_the_weekly_cron(self) -> None:
        crons = {entry["cron"] for entry in triggers(load())["schedule"]}
        assert crons == {CI_DAILY_CRON, CI_WEEKLY_CRON}

    def test_every_cron_a_condition_compares_against_is_declared(self) -> None:
        text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        compared = set(re.findall(r"github\.event\.schedule [!=]= '([^']*)'", text))
        assert compared, "no condition tells the daily cron from the weekly one"
        assert compared <= {CI_DAILY_CRON, CI_WEEKLY_CRON}, (
            f"conditions compare against {sorted(compared)}, which no `on.schedule` entry fires — "
            "that job would never tell the two crons apart."
        )


class TestTheDailyCronIsSlim:
    def test_it_runs_only_the_tree_scans_the_audit_and_the_shuffle(self) -> None:
        assert _ran(github_context("schedule", schedule=CI_DAILY_CRON)) == DAILY_JOBS


class TestTheWeeklyCronIsTheFullRun:
    def test_it_runs_what_a_main_push_runs_plus_the_shuffle_and_the_maintenance(self) -> None:
        weekly = _ran(github_context("schedule", schedule=CI_WEEKLY_CRON))
        assert weekly == _ran(github_context("push")) | {"test-shuffle"} | MAINTENANCE_JOBS


class TestAMainPushKeepsTheFullSuite:
    def test_it_runs_the_shards_and_the_combiner(self) -> None:
        assert _ran(github_context("push")) >= FULL_SUITE

    def test_it_opens_no_durations_refresh(self) -> None:
        assert not _ran(github_context("push")) & MAINTENANCE_JOBS


class TestADispatchedRefreshRunsTheWholeRefresh:
    def test_it_runs_the_full_suite_and_the_refresh(self) -> None:
        dispatched = _ran(github_context("workflow_dispatch"), inputs={"refresh_durations": True})
        assert dispatched >= FULL_SUITE | {"refresh-durations"}


class TestAPullRequestStillReportsEveryRequiredCheck:
    def test_a_same_repo_pull_request_runs_every_required_check_job(self) -> None:
        assert _ran(_PULL_REQUEST, outputs=_HEAVY) >= REQUIRED_CHECK_JOBS


class TestADocsOnlyPullRequestReportsTheTestContextThroughTheShim:
    def test_the_shim_runs_and_the_heavy_lane_does_not(self) -> None:
        docs_only = _ran(_PULL_REQUEST, outputs=_DOCS_ONLY)
        assert "test-shim" in docs_only
        assert not docs_only & {"test", "test-shard", "jscpd-scan", "mutation-diff"}

    def test_every_other_required_check_still_runs(self) -> None:
        assert _ran(_PULL_REQUEST, outputs=_DOCS_ONLY) >= REQUIRED_CHECK_JOBS - {"test"}

    @pytest.mark.parametrize(
        ("github", "outputs", "reporting"),
        [
            (_PULL_REQUEST, _DOCS_ONLY, ["test-shim"]),
            (_PULL_REQUEST, _HEAVY, ["test"]),
            (github_context("push"), {}, ["test"]),
            (github_context("schedule", schedule=CI_WEEKLY_CRON), {}, ["test"]),
            (github_context("schedule", schedule=CI_DAILY_CRON), {}, []),
        ],
        ids=["docs-only-pr", "heavy-pr", "push", "weekly", "daily"],
    )
    def test_exactly_one_job_reports_the_test_context_wherever_the_suite_is_owed(
        self, github: dict[str, Any], outputs: dict[str, dict[str, str]], reporting: list[str]
    ) -> None:
        assert _reporting_as_test(github, outputs=outputs) == reporting
