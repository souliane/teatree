"""The test-shard lane must bin-pack by recorded duration and record the slow slices (#3160).

The shard imbalance #3160 fixed had one root cause: ``dev/.test_durations`` carried no entries
for ``tests/quality/`` or the ``--doctest-modules`` items, so pytest-split ballasted them blindly.
The fix has three load-bearing parts that no other test locks, so a careless edit to the shard
invocation could silently regress the rebalance while every gate still passes:

* ``--splitting-algorithm least_duration`` — bin-packs the recorded durations tighter than the
    default chunk split, which is what turns a balanced ``dev/.test_durations`` into balanced shards.
* ``--doctest-modules`` — the doctest items run in the SAME sharded lane, so their durations are
    measurable there (and their coverage counts toward the combined floor).
* the scheduled ``--store-durations --clean-durations`` record — the weekly lane is where the
    previously-unrecorded ``tests/quality`` + doctest durations are captured for the refresh PR, so
    the committed file stops ballasting them blindly. Recording must NOT happen on PR/push runs
    (that would pay the store write on every PR); it is gated to the weekly cron and an explicit
    refresh dispatch.

Locking the invocation, not the balance itself: the balance is data (``dev/.test_durations``, kept
fresh by the scheduled ``refresh-durations`` job on representative CI hardware), but the flags that
consume that data are code and belong under a regression guard.
"""

from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests._actions_workflow import CI_WEEKLY_CRON, github_context, render

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_LOCAL_TWIN = _REPO_ROOT / "dev" / "ci-shard.sh"


def _shard_runs() -> list[str]:
    jobs = cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])
    return [str(s.get("run", "")) for s in jobs["test-shard"].get("steps", []) if isinstance(s, dict)]


def _shard_run() -> str:
    pytest_runs = [run for run in _shard_runs() if "pytest" in run]
    assert pytest_runs, "test-shard must have a step that runs pytest."
    return pytest_runs[0]


class TestShardSplittingIsLeastDuration:
    def test_shard_uses_least_duration_algorithm(self) -> None:
        assert "--splitting-algorithm least_duration" in _shard_run(), (
            "The shard lane must bin-pack by recorded duration (--splitting-algorithm "
            "least_duration); the default chunk split re-introduces the #3160 imbalance."
        )

    def test_shard_reads_the_committed_durations_file(self) -> None:
        assert "--durations-path dev/.test_durations" in _shard_run(), (
            "The shard lane must split on the committed dev/.test_durations, the file the "
            "scheduled refresh keeps balanced (#3160)."
        )

    def test_shard_collects_doctest_items(self) -> None:
        assert "--doctest-modules" in _shard_run(), (
            "Doctest items must run in the sharded lane so their durations are measured there "
            "and their coverage counts toward the combined floor (#3160)."
        )


class TestScheduledRunRecordsDurations:
    """The slow, previously-unrecorded slices get durations ONLY on the scheduled lane."""

    def test_schedule_stores_clean_durations(self) -> None:
        run = _shard_run()
        assert "--store-durations --clean-durations" in run, (
            "The scheduled shard lane must record fresh durations (including the previously "
            "unrecorded tests/quality + doctest items) for the refresh-durations PR (#3160)."
        )

    @pytest.mark.parametrize(
        ("github", "inputs", "records"),
        [
            (github_context("schedule", schedule=CI_WEEKLY_CRON), {}, True),
            (github_context("workflow_dispatch"), {"refresh_durations": True}, True),
            (github_context("workflow_dispatch"), {"refresh_durations": False}, False),
            (github_context("push"), {}, False),
            (github_context("pull_request", ref="refs/pull/42/merge"), {}, False),
        ],
        ids=["weekly", "refresh-dispatch", "plain-dispatch", "push", "pull-request"],
    )
    def test_only_a_refresh_run_records(self, github: dict[str, Any], inputs: dict[str, Any], *, records: bool) -> None:
        rendered = str(render(_shard_run(), {"github": github, "inputs": inputs}))
        assert ("--store-durations --clean-durations" in rendered) is records, (
            "Duration recording belongs to the weekly cron and an explicit refresh dispatch; recording "
            "on every PR/push would pay the store write on the critical path (#3160)."
        )


class TestTheShardLaneBlocksTheImpactPlugin:
    """tach's pytest plugin analyses every collected item once `main` resolves, and discards it without `--tach`."""

    def test_the_ci_shard_blocks_it(self) -> None:
        assert "-p no:tach" in _shard_run(), (
            "A push to main checks out `main`, which arms tach's plugin without `--tach`: it then "
            "spends minutes per shard on an impact analysis whose answer the lane never reads."
        )

    def test_the_local_twin_blocks_it(self) -> None:
        twin = _LOCAL_TWIN.read_text(encoding="utf-8")
        assert "-p no:tach" in twin[twin.index("exec uv run") :], (
            "dev/ci-shard.sh reproduces the CI shard's flags; a local checkout always has `main`."
        )

    def test_the_blocked_name_is_the_name_tach_registers(self) -> None:
        registered = {ep.name for ep in entry_points(group="pytest11") if ep.value == "tach.pytest_plugin"}
        assert registered == {"tach"}, (
            "pytest ignores `-p no:<name>` for a name nothing registers, so a renamed entry point "
            "would bring the plugin back with every assertion above still green."
        )


class TestAShardRecordsItsRunnerHardware:
    def test_it_prints_the_core_count_before_the_tests_start(self) -> None:
        runs = _shard_runs()
        hardware = [index for index, run in enumerate(runs) if "$(nproc)" in run]
        assert hardware, "Runners come in hardware classes ~1.7x apart; a shard timing is unreadable without its class."
        assert hardware[0] < runs.index(_shard_run())
