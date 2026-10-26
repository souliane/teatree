"""A wrong-head correction names a valid full head only."""

from teatree.core.modelkit.task_failure_taxonomy import WRONG_HEAD_RETRY_PREFIX
from teatree.loop.transient_requeue_correction import wrong_head_retry_head


def test_wrong_head_correction_uses_only_a_full_sha() -> None:
    head = "a" * 40
    assert wrong_head_retry_head(f"{WRONG_HEAD_RETRY_PREFIX}{head}; checkout mismatch") == head
    assert wrong_head_retry_head(f"{WRONG_HEAD_RETRY_PREFIX}a123456; checkout mismatch") == ""
    assert wrong_head_retry_head("recording refused: checkout mismatch") == ""
