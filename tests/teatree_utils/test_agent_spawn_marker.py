import os
from unittest.mock import patch

from teatree.utils.agent_spawn_marker import DISPATCHED_AGENT_ENV, dispatched_agent_env, is_dispatched_agent_process


def test_a_process_carrying_the_spawn_marker_is_a_dispatched_agent() -> None:
    with patch.dict(os.environ, dispatched_agent_env()):
        assert is_dispatched_agent_process()


def test_a_blank_marker_is_not_one() -> None:
    with patch.dict(os.environ, {DISPATCHED_AGENT_ENV: " "}):
        assert not is_dispatched_agent_process()
