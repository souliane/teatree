"""A skipped maintenance job must not be folded into a green scheduled run (#4262).

``refresh-durations`` was skipped on every scheduled run for two weeks. A skipped job does
not red a run, so the lane reported ``success`` while the job did nothing and
``dev/.test_durations`` decayed to 10.8% of the test files. Nothing was wrong with any
individual signal — "the run passed" was true, and "the maintenance job ran" was never asked.

This job asks it. The reporter's own script is executed here over every result pair, because
the state that matters (``skipped``) is exactly the one that produces no failure to observe.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
_BASH = shutil.which("bash") or "/bin/bash"

REPORTER_JOB = "scheduled-maintenance-report"
_MAINTENANCE_JOB = "refresh-durations"


def _jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"])


def _reporter() -> dict[str, Any]:
    jobs = _jobs()
    assert REPORTER_JOB in jobs, (
        f"ci.yml has no `{REPORTER_JOB}` job. A skipped maintenance job does not red a run, so "
        "without one the scheduled lane reports green while the job does nothing (#4262)."
    )
    return cast("dict[str, Any]", jobs[REPORTER_JOB])


def _reporter_script() -> str:
    steps = [step for step in _reporter().get("steps", []) if isinstance(step, dict)]
    runs = [str(step["run"]) for step in steps if step.get("run")]
    assert len(runs) == 1, f"`{REPORTER_JOB}` must be one script, found {len(runs)}."
    return runs[0]


def _run_reporter(*, shards: str, refresh: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Execute the reporter's own script with the job results GitHub would hand it."""
    return subprocess.run(
        [_BASH, "-c", _reporter_script()],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "SHARDS": shards, "REFRESH": refresh},
        cwd=tmp_path,
        check=False,
    )


class TestTheReporterIsReachableOnAScheduledRun:
    def test_it_re_establishes_always_so_an_upstream_skip_does_not_silence_it(self) -> None:
        condition = str(_reporter().get("if", ""))
        assert "always()" in condition, (
            "the reporter needs jobs that themselves run via `always()`, so GitHub propagates their "
            "skip to it unless it says `always()` too — the exact #4048 shape it exists to report."
        )
        assert "github.event_name == 'schedule'" in condition, (
            "the reporter is about the SCHEDULED lane; a PR run has no maintenance job to report on."
        )

    def test_it_watches_the_maintenance_job_and_the_shards_that_gate_it(self) -> None:
        needs = _reporter().get("needs") or []
        declared = [needs] if isinstance(needs, str) else list(needs)
        assert _MAINTENANCE_JOB in declared, f"the reporter must observe `{_MAINTENANCE_JOB}`."
        assert "test-shard" in declared, (
            "the reporter must observe `test-shard` too: a maintenance job skipped because the "
            "shards failed is already explained, and reporting it again would double-red the run."
        )

    def test_the_results_arrive_as_env_not_inlined_expressions(self) -> None:
        step = next(s for s in _reporter()["steps"] if isinstance(s, dict))
        env = cast("dict[str, Any]", step.get("env", {}))
        assert "needs.refresh-durations.result" in str(env.get("REFRESH", ""))
        assert "needs.test-shard.result" in str(env.get("SHARDS", ""))


class TestSkippedIsReportedAsSkipped:
    def test_a_skipped_maintenance_job_reds_an_otherwise_green_lane(self, tmp_path: Path) -> None:
        result = _run_reporter(shards="success", refresh="skipped", tmp_path=tmp_path)
        assert result.returncode != 0, (
            "every shard passed and the maintenance job did not run — the state that was invisible "
            "for two weeks. It must red the run, not be folded into green (#4262)."
        )
        assert "::error::" in result.stderr
        assert _MAINTENANCE_JOB in result.stderr, "the report must name the job that did not run."

    def test_a_cancelled_maintenance_job_is_reported_the_same_way(self, tmp_path: Path) -> None:
        result = _run_reporter(shards="success", refresh="cancelled", tmp_path=tmp_path)
        assert result.returncode != 0, "cancelled is as invisible as skipped — neither reds a run."

    def test_a_maintenance_job_that_ran_and_passed_is_green(self, tmp_path: Path) -> None:
        result = _run_reporter(shards="success", refresh="success", tmp_path=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "::error::" not in result.stderr

    def test_a_maintenance_job_that_ran_and_failed_is_left_to_its_own_red(self, tmp_path: Path) -> None:
        result = _run_reporter(shards="success", refresh="failure", tmp_path=tmp_path)
        assert result.returncode == 0, (
            "a failed maintenance job already reds the run honestly; a second red from the reporter "
            "adds a failure that names no new cause."
        )

    @pytest.mark.parametrize("refresh", ["skipped", "failure", "success"])
    def test_a_shard_failure_is_the_cause_and_is_not_reported_twice(self, refresh: str, tmp_path: Path) -> None:
        result = _run_reporter(shards="failure", refresh=refresh, tmp_path=tmp_path)
        assert result.returncode == 0, (
            "the maintenance job is gated on the shards passing, so a shard failure already explains "
            "and reds this run — the reporter must not attribute it to the maintenance job."
        )
        assert "::error::" not in result.stderr
