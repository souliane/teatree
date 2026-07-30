"""The resurrected loopback ttyd launcher + claude-argv builder (#3162)."""

from subprocess import DEVNULL, PIPE
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


def test_launch_ttyd_does_not_leave_an_unread_stderr_pipe() -> None:
    # DASH-3: nothing ever reads the ttyd process's stderr, so a PIPE could fill the
    # OS pipe buffer and block a chatty ttyd mid-session. Both streams go to DEVNULL.
    proc = MagicMock(pid=999)
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "find_free_port", return_value=45678),
        patch.object(terminal_launcher, "spawn", return_value=proc) as spawn,
    ):
        terminal_launcher.launch_ttyd(["claude"])
    assert spawn.call_args.kwargs["stderr"] == DEVNULL
    assert spawn.call_args.kwargs["stderr"] != PIPE
    assert spawn.call_args.kwargs["stdout"] == DEVNULL


def test_launch_ttyd_missing_binary_returns_error_not_raise() -> None:
    with patch.object(terminal_launcher.shutil, "which", return_value=None):
        result = terminal_launcher.launch_ttyd(["claude"])
    assert result.launch_url == ""
    assert "ttyd not found" in result.error


def test_launch_ttyd_arms_a_connect_grace_reaper() -> None:
    # DASH-4: `--once` only bounds a CONNECTED ttyd (it exits on disconnect); a ttyd
    # nobody ever connects to would listen on loopback forever. `launch_ttyd` must arm
    # a grace-window reaper so an unconnected one is bounded.
    proc = MagicMock(pid=999)
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "find_free_port", return_value=45678),
        patch.object(terminal_launcher, "spawn", return_value=proc),
        patch.object(terminal_launcher.threading, "Timer") as timer_cls,
    ):
        terminal_launcher.launch_ttyd(["claude"])
    timer_cls.assert_called_once_with(
        terminal_launcher._CONNECT_GRACE_SECONDS,
        terminal_launcher._reap_if_unconnected,
        args=(proc, 45678),
    )
    timer = timer_cls.return_value
    assert timer.daemon is True
    timer.start.assert_called_once_with()


def test_reap_if_unconnected_terminates_an_orphan() -> None:
    proc = MagicMock()
    proc.poll.return_value = None  # still running
    with patch.object(terminal_launcher, "_has_established_client", return_value=False):
        terminal_launcher._reap_if_unconnected(proc, 45678)
    proc.terminate.assert_called_once_with()


def test_reap_if_unconnected_spares_a_live_session() -> None:
    proc = MagicMock()
    proc.poll.return_value = None
    with patch.object(terminal_launcher, "_has_established_client", return_value=True):
        terminal_launcher._reap_if_unconnected(proc, 45678)
    proc.terminate.assert_not_called()


def test_reap_if_unconnected_is_a_noop_once_ttyd_already_exited() -> None:
    proc = MagicMock()
    proc.poll.return_value = 0  # already exited via --once
    with patch.object(terminal_launcher, "_has_established_client") as probe:
        terminal_launcher._reap_if_unconnected(proc, 45678)
    proc.terminate.assert_not_called()
    probe.assert_not_called()


def test_has_established_client_fails_safe_when_probe_errors() -> None:
    # A live session must never be reaped on an inconclusive probe → assume connected.
    with patch.object(terminal_launcher, "run_allowed_to_fail", side_effect=FileNotFoundError("lsof")):
        assert terminal_launcher._has_established_client(45678) is True


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
