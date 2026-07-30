"""The test-shard lane must bin-pack by recorded duration and record the slow slices (#3160).

The shard imbalance #3160 fixed had one root cause: ``dev/.test_durations`` carried no entries
for ``tests/quality/`` or the ``--doctest-modules`` items, so pytest-split ballasted them blindly.
The fix has three load-bearing parts that no other test locks, so a careless edit to the shard
invocation could silently regress the rebalance while every gate still passes:

* ``--splitting-algorithm least_duration`` — bin-packs the recorded durations tighter than the
    default chunk split, which is what turns a balanced ``dev/.test_durations`` into balanced shards.
* ``--doctest-modules`` — the doctest items run in the SAME sharded lane, so their durations are
    measurable there (and their coverage counts toward the combined floor).
* the scheduled ``--store-durations --clean-durations`` record — the daily lane is where the
    previously-unrecorded ``tests/quality`` + doctest durations are captured for the refresh PR, so
    the committed file stops ballasting them blindly. Recording must NOT happen on PR/push runs
    (that would pay the store write on every PR); it is gated to the ``schedule`` event only.

Locking the invocation, not the balance itself: the balance is data (``dev/.test_durations``, kept
fresh by the scheduled ``refresh-durations`` job on representative CI hardware), but the flags that
consume that data are code and belong under a regression guard.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _shard_run() -> str:
    jobs = cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])
    steps = [s for s in jobs["test-shard"].get("steps", []) if isinstance(s, dict)]
    pytest_steps = [s for s in steps if "pytest" in str(s.get("run", ""))]
    assert pytest_steps, "test-shard must have a step that runs pytest."
    return str(pytest_steps[0]["run"])


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

    def test_recording_is_gated_to_the_schedule_event_only(self) -> None:
        run = _shard_run()
        assert "github.event_name == 'schedule'" in run, (
            "Duration recording must be gated to the daily schedule; recording on every PR/push "
            "would pay the store write on the critical path (#3160)."
        )
