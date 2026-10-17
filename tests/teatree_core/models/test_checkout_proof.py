from teatree.core.models.types import validated_worktree_extra


def test_deferred_checkout_exit_is_a_recognized_worktree_proof() -> None:
    record = {
        "holder": "task:5297",
        "session": "run",
        "digest": "state",
        "cause": "process_table",
        "at": "2026-10-16T00:00:00+00:00",
    }
    assert validated_worktree_extra({"deferred_exit": record})["deferred_exit"] == record
