"""The checkout server is isolated and tears down its whole process group."""

import contextlib
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import teatree.visual_qa_server as server_module
from teatree.types import RunCommand
from teatree.visual_qa_server import ServerSpec, VisualQAUnavailableError, served_app

LISTEN = (
    "import os,socket,time;"
    "s=socket.socket();s.bind(('127.0.0.1',int(os.environ['T3_VISUAL_QA_PORT'])));s.listen();time.sleep(30)"
)
INHERITED_OVERRIDES = ("DJANGO_SETTINGS_MODULE", "T3_CONFIG_DB", "T3_CONTROL_DB_DIR", "T3_DATA_DIR", "DATABASE_URL")


def _spec(tmp_path: Path, script: str, *args: str, cwd: Path | None = None) -> ServerSpec:
    command = RunCommand(args=[sys.executable, "-c", script, *args], cwd=cwd or tmp_path)
    return ServerSpec(command, str(tmp_path), lambda: None)


def test_sandbox_does_not_change_the_live_config_database(tmp_path: Path, monkeypatch) -> None:
    live_db = tmp_path / "live.sqlite3"
    connection = sqlite3.connect(live_db)
    try:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.commit()
    finally:
        connection.close()
    before = live_db.read_bytes()
    for key in INHERITED_OVERRIDES:
        monkeypatch.setenv(key, str(live_db))
    names = (*INHERITED_OVERRIDES, "XDG_DATA_HOME", "TMPDIR")
    script = f"import os;print(*(os.environ.get(k,'absent') for k in {names!r}),flush=True);{LISTEN}"

    with served_app(_spec(tmp_path, script)) as app:
        scratch = str(app.log_path.parent)
        assert app.log_path.read_text().split() == ["absent"] * len(INHERITED_OVERRIDES) + [scratch, scratch]
        assert app.base_url.startswith("http://127.0.0.1:")

    assert live_db.read_bytes() == before


@pytest.mark.parametrize("declared_cwd", [True, False])
def test_server_starts_in_its_declared_directory_with_its_declared_env(tmp_path: Path, *, declared_cwd: bool) -> None:
    declared, fallback = tmp_path / "declared", tmp_path / "fallback"
    declared.mkdir()
    fallback.mkdir()
    script = f"import os;print(os.getcwd(),os.environ['T3_PROBE'],flush=True);{LISTEN}"
    command = RunCommand(
        args=[sys.executable, "-c", script], cwd=declared if declared_cwd else None, env={"T3_PROBE": "declared-env"}
    )

    with served_app(ServerSpec(command, str(fallback), lambda: None)) as app:
        expected = declared if declared_cwd else fallback
        assert app.log_path.read_text().split() == [str(expected.resolve()), "declared-env"]


def test_prepare_runs_before_the_server_starts(tmp_path: Path) -> None:
    prepared = tmp_path / "prepared"
    script = f"import os,sys;print(os.path.exists(sys.argv[1]),flush=True);{LISTEN}"
    spec = _spec(tmp_path, script, str(prepared))

    with served_app(ServerSpec(spec.command, spec.repo_path, prepared.touch)) as app:
        assert app.log_path.read_text().split() == ["True"]


def test_a_server_that_exits_reports_the_redacted_tail_of_its_log(tmp_path: Path) -> None:
    script = "import sys;print(*(f'line {n}' for n in range(20)),'API_TOKEN=hunter2secret',sep='\\n');sys.exit(3)"

    with pytest.raises(VisualQAUnavailableError) as error, served_app(_spec(tmp_path, script)):
        pass

    message = str(error.value)
    assert message.startswith("server exited 3: ")
    assert "API_TOKEN=<redacted>" in message
    assert "hunter2secret" not in message
    assert "line 9" in message
    assert "line 8" not in message


def test_a_server_that_never_listens_is_did_not_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server_module, "SERVER_START_TIMEOUT_S", 1)

    with (
        pytest.raises(VisualQAUnavailableError, match="did not listen within 1s"),
        served_app(_spec(tmp_path, "import time;time.sleep(30)")),
    ):
        pass


def test_a_server_that_cannot_be_spawned_is_did_not_run(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "pass", cwd=tmp_path / "missing")

    with pytest.raises(VisualQAUnavailableError, match="could not start"), served_app(spec):
        pass


def test_server_does_not_share_the_gate_stdin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server_module, "SERVER_START_TIMEOUT_S", 3)

    with served_app(_spec(tmp_path, f"import os;os.read(0,1);{LISTEN}")):
        pass


def test_a_server_ignoring_sigterm_is_killed_after_the_grace_period(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server_module, "STOP_GRACE_S", 0.5)
    script = f"import os,signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);print(os.getpid(),flush=True);{LISTEN}"

    with served_app(_spec(tmp_path, script)) as app:
        server_pid = int(app.log_path.read_text().split()[0])

    assert _gone_within(server_pid, 3)


def test_sigterm_resistant_grandchild_is_reaped(tmp_path: Path) -> None:
    pid_file, ready_file = tmp_path / "child.pid", tmp_path / "child.ready"
    child_script = (
        "import signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);open(sys.argv[1],'w');time.sleep(30)"
    )
    parent_script = """
import os, socket, subprocess, sys, time
pid_file, ready_file, child_script = sys.argv[1:4]
child = subprocess.Popen([sys.executable, '-c', child_script, ready_file])
open(pid_file, 'w').write(str(child.pid))
while not os.path.exists(ready_file):
    time.sleep(0.02)
s = socket.socket()
s.bind(('127.0.0.1', int(os.environ['T3_VISUAL_QA_PORT'])))
s.listen()
time.sleep(30)
"""
    spec = _spec(tmp_path, parent_script, str(pid_file), str(ready_file), child_script)

    with served_app(spec):
        child_pid = int(pid_file.read_text())
        assert _live_process(child_pid)

    assert _gone_within(child_pid, 3)


def test_wrapper_reaps_child_before_reporting_stopped(tmp_path: Path) -> None:
    marker = tmp_path / "reaped"
    script = """
import os, signal, socket, sys, time
from pathlib import Path

def finish(_signum, _frame):
    time.sleep(0.3)
    Path(sys.argv[1]).write_text('done')
    raise SystemExit(0)

signal.signal(signal.SIGTERM, finish)
s = socket.socket()
s.bind(('127.0.0.1', int(os.environ['T3_VISUAL_QA_PORT'])))
s.listen()
time.sleep(30)
"""
    with served_app(_spec(tmp_path, script, str(marker))):
        pass

    assert marker.read_text() == "done"


def test_server_stops_when_gate_process_is_killed(tmp_path: Path) -> None:
    gate_script = """
import sys
import time
from pathlib import Path
from teatree.types import RunCommand
from teatree.visual_qa_server import ServerSpec, served_app

server = (
    "import os,socket,time; s=socket.socket();"
    "s.bind(('127.0.0.1',int(os.environ['T3_VISUAL_QA_PORT'])));"
    "s.listen(); time.sleep(30)"
)
spec = ServerSpec(RunCommand(args=[sys.executable, '-c', server], cwd=Path(sys.argv[1])), sys.argv[1], lambda: None)
with served_app(spec) as app:
    print(app.base_url, app.pgid, flush=True)
    time.sleep(30)
"""
    gate = subprocess.Popen(
        [sys.executable, "-c", gate_script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
    )
    assert gate.stdout is not None
    try:
        address = gate.stdout.readline().split()
        assert len(address) == 2, gate.stderr.read() if gate.stderr else ""
        port = int(address[0].rsplit(":", 1)[1])
        pgid = int(address[1])
        gate.kill()
        gate.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    time.sleep(0.05)
            except OSError:
                break
        else:
            message = "checkout server survived its gate process"
            raise AssertionError(message)
        assert _gone_within(pgid, 5)
    finally:
        if gate.poll() is None:
            gate.kill()
            gate.wait(timeout=5)
        gate.stdout.close()
        if gate.stderr is not None:
            gate.stderr.close()
        if "pgid" in locals():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while _live_process(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _live_process(pid)


def _live_process(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False
