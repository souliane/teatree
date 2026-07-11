"""The resurrected loopback ttyd launcher + claude-argv builder (#3162)."""

from unittest.mock import MagicMock, patch

import pytest

from teatree.agents import terminal_launcher, web_terminal

_UUID = "12345678-1234-1234-1234-123456789abc"


def test_launch_ttyd_spawns_writable_once_on_loopback() -> None:
    proc = MagicMock(pid=999)
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "find_free_port", return_value=45678),
        patch.object(terminal_launcher, "spawn", return_value=proc) as spawn,
    ):
        result = terminal_launcher.launch_ttyd(["claude"])
    argv = spawn.call_args.args[0]
    assert "--writable" in argv
    assert "--once" in argv
    assert "127.0.0.1" in argv
    assert result.launch_url == "http://127.0.0.1:45678"
    assert result.pid == 999


def test_launch_ttyd_missing_binary_returns_error_not_raise() -> None:
    with patch.object(terminal_launcher.shutil, "which", return_value=None):
        result = terminal_launcher.launch_ttyd(["claude"])
    assert result.launch_url == ""
    assert "ttyd not found" in result.error


def test_build_claude_command_fresh_session() -> None:
    with patch.object(web_terminal.shutil, "which", return_value="/usr/bin/claude"):
        assert web_terminal.build_claude_command() == ["/usr/bin/claude"]


def test_build_claude_command_resume_with_valid_uuid() -> None:
    with patch.object(web_terminal.shutil, "which", return_value="/usr/bin/claude"):
        assert web_terminal.build_claude_command(_UUID) == ["/usr/bin/claude", "--resume", _UUID]


def test_build_claude_command_rejects_non_uuid_resume() -> None:
    with (
        patch.object(web_terminal.shutil, "which", return_value="/usr/bin/claude"),
        pytest.raises(ValueError, match="valid claude session"),
    ):
        web_terminal.build_claude_command("; rm -rf /")


def test_build_claude_command_missing_claude_raises() -> None:
    with patch.object(web_terminal.shutil, "which", return_value=None), pytest.raises(FileNotFoundError):
        web_terminal.build_claude_command()
