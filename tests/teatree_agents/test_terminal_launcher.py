"""The resurrected loopback ttyd launcher + claude-argv builder (#3162)."""

import socket
import time
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from teatree.agents import terminal_launcher, web_terminal
from teatree.llm.credentials import CredentialError

_UUID = "12345678-1234-1234-1234-123456789abc"


def test_launch_ttyd_spawns_writable_once_on_loopback() -> None:
    proc = MagicMock(pid=999)
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "_port_available", return_value=True),
        patch.object(terminal_launcher, "_wait_until_listening", return_value=True),
        patch.object(terminal_launcher, "spawn", return_value=proc) as spawn,
    ):
        result = terminal_launcher.launch_ttyd(["claude"])
    argv = spawn.call_args.args[0]
    assert "--writable" in argv
    assert "--once" in argv
    assert "--check-origin" in argv
    assert "127.0.0.1" in argv
    assert result.pid == 999


def test_terminal_uses_a_stable_port_for_the_browser_tunnel() -> None:
    proc = MagicMock(pid=999)
    proc.poll.return_value = None
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "_port_available", return_value=True),
        patch.object(terminal_launcher, "_wait_until_listening", return_value=True),
        patch.object(terminal_launcher, "spawn", return_value=proc),
    ):
        result = terminal_launcher.launch_ttyd(["claude"])
    assert result.launch_url == "http://127.0.0.1:8804"


def test_terminal_does_not_report_a_url_when_listener_never_starts() -> None:
    proc = MagicMock(pid=999)
    proc.poll.return_value = None
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "_port_available", return_value=True),
        patch.object(terminal_launcher, "spawn", return_value=proc),
        patch.object(socket, "create_connection", side_effect=OSError("connection refused")),
    ):
        result = terminal_launcher.launch_ttyd(["claude"])
    assert result.launch_url == ""
    assert "listen" in result.error


def test_terminal_refuses_an_occupied_port() -> None:
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "_port_available", return_value=False),
        patch.object(terminal_launcher, "spawn") as spawn,
    ):
        result = terminal_launcher.launch_ttyd(["claude"])
    assert result.launch_url == ""
    assert "already in use" in result.error
    spawn.assert_not_called()


def test_terminal_port_is_available_after_the_previous_client_closes() -> None:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as client:
            peer, _ = listener.accept()
            peer.close()
            client.recv(1)
    assert terminal_launcher._port_available(port)


def test_terminal_reaps_a_child_that_exits() -> None:
    proc = Popen(["/bin/true"], stdout=DEVNULL, stderr=DEVNULL, text=True)
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "_port_available", return_value=True),
        patch.object(terminal_launcher, "_wait_until_listening", return_value=True),
        patch.object(terminal_launcher, "_arm_connect_grace_reaper"),
        patch.object(terminal_launcher, "spawn", return_value=proc),
    ):
        result = terminal_launcher.launch_ttyd(["claude"])
    deadline = time.monotonic() + 2
    while proc.returncode is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert result.launch_url
    assert proc.returncode == 0


def test_deploy_image_installs_the_terminal_connection_probe() -> None:
    dockerfile = Path(__file__).resolve().parents[2] / "deploy" / "Dockerfile"
    package_line = next(line for line in dockerfile.read_text(encoding="utf-8").splitlines() if "sqlite3 ttyd" in line)
    assert " lsof " in package_line


def test_launch_ttyd_does_not_leave_an_unread_stderr_pipe() -> None:
    # DASH-3: nothing ever reads the ttyd process's stderr, so a PIPE could fill the
    # OS pipe buffer and block a chatty ttyd mid-session. Both streams go to DEVNULL.
    proc = MagicMock(pid=999)
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "_port_available", return_value=True),
        patch.object(terminal_launcher, "_wait_until_listening", return_value=True),
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
        patch.object(terminal_launcher, "_port_available", return_value=True),
        patch.object(terminal_launcher, "_wait_until_listening", return_value=True),
        patch.object(terminal_launcher, "spawn", return_value=proc),
        patch.object(terminal_launcher.threading, "Timer") as timer_cls,
    ):
        terminal_launcher.launch_ttyd(["claude"])
    timer_cls.assert_called_once_with(
        terminal_launcher._CONNECT_GRACE_SECONDS,
        terminal_launcher._reap_if_unconnected,
        args=(proc, 8804),
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


class TestTheWebTerminalRidesTheSameCredentialADispatchGets:
    """The dashboard's `claude` was the ONE spawn resolving no credential of its own.

    A dispatched agent gets its Layer-2 credential through `system_child_env()`;
    this spawn passed no env at all, so it inherited whatever ambient auth the dash
    process happened to hold — a different account from the one routing picked, or
    none at all.
    """

    _ENV: ClassVar[dict[str, str]] = {"CLAUDE_CODE_OAUTH_TOKEN": "resolved-by-the-selector", "PATH": "/usr/bin"}

    @staticmethod
    def _spawn_for(child_env: object) -> MagicMock:
        proc = MagicMock(pid=999)
        with (
            patch.object(web_terminal.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
            patch.object(terminal_launcher, "_port_available", return_value=True),
            patch.object(terminal_launcher, "_wait_until_listening", return_value=True),
            patch.object(terminal_launcher, "spawn", return_value=proc) as spawn,
            patch.object(terminal_launcher, "_arm_connect_grace_reaper"),
            patch.object(web_terminal, "system_child_env", side_effect=child_env),
        ):
            web_terminal.launch_web_session()
        return spawn

    def test_the_resolved_credential_env_reaches_the_spawn(self) -> None:
        spawn = self._spawn_for(lambda: dict(self._ENV))

        assert spawn.call_args.kwargs["env"] == self._ENV

    def test_no_layer2_pin_still_inherits_the_ambient_environment(self) -> None:
        # `system_child_env()` returns None for "no explicit pin — use the ambient env
        # unchanged", and `Popen(env=None)` is exactly that, so an operator who
        # configured no provider keeps the behaviour they had.
        spawn = self._spawn_for(lambda: None)

        assert spawn.call_args.kwargs["env"] is None

    def test_an_unresolvable_credential_renders_a_hint_instead_of_raising(self) -> None:
        # The dash view turns an exception into a 500; a missing credential is an
        # operator-fixable gap and degrades the way a missing ttyd binary does.
        with (
            patch.object(web_terminal.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(web_terminal, "system_child_env", side_effect=CredentialError("no token anywhere")),
        ):
            result = web_terminal.launch_web_session()

        assert result.launch_url == ""
        assert "no token anywhere" in result.error

    def test_the_token_is_never_written_into_the_argv(self) -> None:
        spawn = self._spawn_for(lambda: dict(self._ENV))

        assert "resolved-by-the-selector" not in " ".join(spawn.call_args.args[0])


def test_launch_ttyd_without_an_env_inherits_the_parent_environment() -> None:
    """The launcher stays credential-agnostic — resolution belongs to its caller."""
    proc = MagicMock(pid=999)
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "_port_available", return_value=True),
        patch.object(terminal_launcher, "_wait_until_listening", return_value=True),
        patch.object(terminal_launcher, "spawn", return_value=proc) as spawn,
        patch.object(terminal_launcher, "_arm_connect_grace_reaper"),
    ):
        terminal_launcher.launch_ttyd(["claude"])

    assert spawn.call_args.kwargs["env"] is None
