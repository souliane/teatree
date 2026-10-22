import pytest

from teatree.agents import live_control

NEEDS_LIVE_FENCE = pytest.mark.skipif(
    getattr(live_control, "LiveFence", None) is None,
    reason="the task-attempt fence is supplied by the Codex mailbox change",
)
