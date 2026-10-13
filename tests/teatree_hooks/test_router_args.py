"""Router CLI arguments remain stable after the parser moves to its own module."""

from unittest.mock import patch

from hooks.scripts.router_args import parse_args


def test_parse_event_argument() -> None:
    with patch("sys.argv", ["hook_router.py", "--event", "PreToolUse"]):
        assert parse_args().event == "PreToolUse"
