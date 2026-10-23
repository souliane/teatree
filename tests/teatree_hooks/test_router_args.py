"""Router CLI arguments remain stable after the parser moves to its own module."""

from unittest.mock import patch

from hooks.scripts.hook_router import _FILE_PATH_TOOLS
from hooks.scripts.router_args import parse_args, read_input


def test_router_keeps_file_path_tool_reexport() -> None:
    assert {"Edit", "Write"} == _FILE_PATH_TOOLS


def test_parse_event_argument() -> None:
    with patch("sys.argv", ["hook_router.py", "--event", "PreToolUse"]):
        assert parse_args().event == "PreToolUse"


def test_read_input_preserves_json_and_bad_input_behavior() -> None:
    with patch("sys.stdin.read", return_value='{"event":"PreToolUse"}'):
        assert read_input() == {"event": "PreToolUse"}
    with patch("sys.stdin.read", return_value="{"):
        assert read_input() == {}
