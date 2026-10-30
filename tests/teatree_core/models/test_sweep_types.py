"""The persisted sweep checkpoint has a fixed shape for retry recovery."""

from teatree.core.models.sweep_types import SweepPendingRun


def test_pending_sweep_checkpoint_requires_every_replay_input() -> None:
    assert SweepPendingRun.__required_keys__ == {
        "task_id",
        "run_id",
        "examined_count",
        "external_count",
        "folds",
        "gaps",
    }
