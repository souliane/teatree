"""The resurrected loopback ttyd launcher + claude-argv builder (#3162)."""

import os
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from subprocess import DEVNULL, PIPE
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from teatree.agents import terminal_launcher, web_terminal
from teatree.llm.credentials import CredentialError


def test_launch_uses_fixed_hardened_terminal_route() -> None:
    proc = MagicMock(pid=999)
    with (
        patch.object(terminal_launcher.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
        patch.object(terminal_launcher, "_port_available", return_value=True),
        patch.object(terminal_launcher, "spawn", return_value=proc) as spawn,
        patch.object(terminal_launcher, "_wait_for_ready", return_value=True),
        patch.object(terminal_launcher, "_arm_connect_grace_reaper"),
    ):
        result = terminal_launcher.launch_ttyd(["claude"])
    argv = spawn.call_args.args[0]
    assert argv[argv.index("--port") + 1] == "8804"
    assert argv[argv.index("--interface") + 1] == "127.0.0.1"
    assert {"--writable", "--once", "--check-origin"} <= set(argv)
    assert argv[argv.index("--max-clients") + 1] == "1"
    path = argv[argv.index("--base-path") + 1]
    assert path.startswith("/")
    assert len(path) >= 17
    assert result.launch_url == f"http://127.0.0.1:8804{path}/"
    assert result.pid == 999


def test_launch_refuses_without_lsof() -> None:
    with (
        patch.object(
            terminal_launcher.shutil, "which", side_effect=lambda name: None if name == "lsof" else "/bin/ttyd"
        ),
        patch.object(terminal_launcher, "spawn") as spawn,
    ):
        result = terminal_launcher.launch_ttyd(["claude"])
    assert "lsof" in result.error
    spawn.assert_not_called()


_UUID = "12345678-1234-1234-1234-123456789abc"


def test_launch_ttyd_does_not_leave_an_unread_stderr_pipe() -> None:
    # DASH-3: nothing ever reads the ttyd process's stderr, so a PIPE could fill the
    # OS pipe buffer and block a chatty ttyd mid-session. Both streams go to DEVNULL.
    proc = MagicMock(pid=999)
    with (
        patch.object(terminal_launcher.shutil, "which", return_value="/usr/bin/ttyd"),
        patch.object(terminal_launcher, "_port_available", return_value=True),
        patch.object(terminal_launcher, "spawn", return_value=proc) as spawn,
        patch.object(terminal_launcher, "_wait_for_ready", return_value=True),
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


def _free_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def _recording_spawn(processes: list[subprocess.Popen[str]]) -> Callable[..., subprocess.Popen[str]]:
    original_spawn = terminal_launcher.spawn

    def record(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        process = original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    return record


def _wait_until(condition: Callable[[], bool], *, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def test_exited_ttyd_is_reaped_without_waiting_for_grace(tmp_path: Path) -> None:
    ttyd = tmp_path / "ttyd"
    ttyd.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ttyd.chmod(0o755)
    processes: list[subprocess.Popen[str]] = []
    with (
        patch.object(
            terminal_launcher.shutil, "which", side_effect=lambda name: str(ttyd) if name == "ttyd" else "/usr/bin/lsof"
        ),
        patch.object(terminal_launcher, "spawn", side_effect=_recording_spawn(processes)),
    ):
        result = terminal_launcher.launch_ttyd(["claude"], port=_free_port())
    assert "could not start" in result.error
    assert _wait_until(lambda: processes[0].returncode is not None)
    assert processes[0].returncode == 0


def test_busy_port_does_not_spawn_a_second_terminal() -> None:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with (
            patch.object(terminal_launcher.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.object(terminal_launcher, "spawn") as spawn,
        ):
            result = terminal_launcher.launch_ttyd(["claude"], port=port)
    assert "busy" in result.error
    spawn.assert_not_called()


def _port_left_in_time_wait() -> int:
    with socket.socket() as listener:
        listener.setsockopt(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
        )  # ttyd's listener sets it; its TIME_WAIT inherits
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as client:
            listener.accept()[0].close()
            assert client.recv(1) == b""
    return port


def test_finished_session_does_not_leave_the_port_reading_busy() -> None:
    port = _port_left_in_time_wait()
    proc = MagicMock(pid=999)
    with (
        patch.object(terminal_launcher.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
        patch.object(terminal_launcher, "spawn", return_value=proc) as spawn,
        patch.object(terminal_launcher, "_wait_for_ready", return_value=True),
        patch.object(terminal_launcher, "_arm_connect_grace_reaper"),
    ):
        result = terminal_launcher.launch_ttyd(["claude"], port=port)
    assert result.error == ""
    spawn.assert_called_once()


def test_terminal_that_never_becomes_ready_is_stopped() -> None:
    proc = MagicMock(pid=999)
    proc.poll.return_value = None
    with (
        patch.object(terminal_launcher.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
        patch.object(terminal_launcher, "_port_available", return_value=True),
        patch.object(terminal_launcher, "spawn", return_value=proc),
        patch.object(terminal_launcher, "_arm_connect_grace_reaper"),
        patch.object(terminal_launcher, "_wait_for_ready", return_value=False),
    ):
        result = terminal_launcher.launch_ttyd(["claude"])
    assert "could not start" in result.error
    assert result.launch_url == ""
    proc.terminate.assert_called_once_with()


_FAKE_TTYD = (
    "#!/usr/bin/env python3\n"
    "import http.server, sys\n"
    "class Handler(http.server.BaseHTTPRequestHandler):\n"
    " def do_GET(self):\n"
    "  self.send_response(200)\n"
    "  self.end_headers()\n"
    " def log_message(self, *args): pass\n"
    "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
    "http.server.HTTPServer(('127.0.0.1', port), Handler).{serve}()\n"
)


def _install_fake_ttyd_and_lsof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, serve: str, client_connected: bool = False
) -> Path:
    """Put a fake ttyd and a fake lsof first on PATH; return the file lsof touches when probed."""
    probed = tmp_path / "probed"
    ttyd = tmp_path / "ttyd"
    ttyd.write_text(_FAKE_TTYD.format(serve=serve), encoding="utf-8")
    ttyd.chmod(0o755)
    lsof = tmp_path / "lsof"
    lsof.write_text(
        f"#!/bin/sh\ntouch {probed}\n" + ("printf 'connected\\n'\n" if client_connected else "exit 1\n"),
        encoding="utf-8",
    )
    lsof.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return probed


@pytest.mark.parametrize("connected", [False, True])
def test_supervisor_reaps_idle_terminal_and_spares_connected_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, connected: bool
) -> None:
    probed = _install_fake_ttyd_and_lsof(tmp_path, monkeypatch, serve="serve_forever", client_connected=connected)
    processes: list[subprocess.Popen[str]] = []
    with (
        patch.object(terminal_launcher, "spawn", side_effect=_recording_spawn(processes)),
        patch.object(terminal_launcher, "_CONNECT_GRACE_SECONDS", 1.0),
    ):
        result = terminal_launcher.launch_ttyd(["/bin/sh"], port=_free_port())
        try:
            assert result.pid == processes[0].pid
            assert _wait_until(probed.exists), "the supervisor never probed for a client"
            if connected:
                assert processes[0].returncode is None
            else:
                assert _wait_until(lambda: processes[0].returncode is not None)
        finally:
            if processes[0].returncode is None:
                processes[0].terminate()
                processes[0].wait(timeout=5)


def test_supervisor_collects_a_terminal_that_exits_after_becoming_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ttyd_and_lsof(tmp_path, monkeypatch, serve="handle_request")
    processes: list[subprocess.Popen[str]] = []
    with (
        patch.object(terminal_launcher, "spawn", side_effect=_recording_spawn(processes)),
        patch.object(terminal_launcher, "_CONNECT_GRACE_SECONDS", 60.0),
    ):
        result = terminal_launcher.launch_ttyd(["/bin/sh"], port=_free_port())
    assert result.launch_url
    assert _wait_until(lambda: processes[0].returncode == 0)


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
            patch.object(terminal_launcher, "spawn", return_value=proc) as spawn,
            patch.object(terminal_launcher, "_wait_for_ready", return_value=True),
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
        patch.object(terminal_launcher, "spawn", return_value=proc) as spawn,
        patch.object(terminal_launcher, "_wait_for_ready", return_value=True),
        patch.object(terminal_launcher, "_arm_connect_grace_reaper"),
    ):
        terminal_launcher.launch_ttyd(["claude"])

    assert spawn.call_args.kwargs["env"] is None
