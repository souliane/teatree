from unittest.mock import patch

from teatree.utils.agent_spawn_marker import dispatched_agent_env, is_dispatched_agent_process


def test_spawn_marker_names_one_environment_key() -> None:
    assert dispatched_agent_env() == {"T3_DISPATCHED_AGENT": "1"}


def test_process_marker_requires_a_nonblank_value() -> None:
    with patch.dict("os.environ", {"T3_DISPATCHED_AGENT": "1"}):
        assert is_dispatched_agent_process()
    with patch.dict("os.environ", {"T3_DISPATCHED_AGENT": " "}):
        assert not is_dispatched_agent_process()
