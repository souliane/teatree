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

from teatree.types import RunCommand
from teatree.visual_qa_server import ServerSpec, served_app


def test_sandbox_does_not_change_the_live_config_database(tmp_path: Path, monkeypatch) -> None:
    live_db = tmp_path / "live.sqlite3"
    connection = sqlite3.connect(live_db)
    try:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.commit()
    finally:
        connection.close()
    before = live_db.read_bytes()
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "teatree.settings")
    monkeypatch.setenv("T3_CONFIG_DB", str(live_db))
    script = (
        "import os,socket,time;"
        "print(os.environ.get('DJANGO_SETTINGS_MODULE','absent'),"
        "os.environ.get('T3_CONFIG_DB','absent'),"
        "os.environ.get('XDG_DATA_HOME','absent'),"
        "os.environ.get('TMPDIR','absent'),flush=True);"
        "s=socket.socket();s.bind(('127.0.0.1',int(os.environ['T3_VISUAL_QA_PORT'])));"
        "s.listen();time.sleep(30)"
    )
    spec = ServerSpec(RunCommand(args=[sys.executable, "-c", script], cwd=tmp_path), str(tmp_path), lambda: None)

    with served_app(spec) as app:
        assert f"absent absent {app.log_path.parent} {app.log_path.parent}" in app.log_path.read_text()

    assert live_db.read_bytes() == before


def test_sigterm_resistant_grandchild_is_reaped(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    child_script = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"
    parent_script = (
        "import os,socket,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]);"
        "open(sys.argv[1],'w').write(str(child.pid));"
        "s=socket.socket();s.bind(('127.0.0.1',int(os.environ['T3_VISUAL_QA_PORT'])));"
        "s.listen();time.sleep(30)"
    )
    spec = ServerSpec(
        RunCommand(args=[sys.executable, "-c", parent_script, str(child_pid_file), child_script], cwd=tmp_path),
        str(tmp_path),
        lambda: None,
    )

    with served_app(spec):
        child_pid = int(child_pid_file.read_text())
        assert _live_process(child_pid)

    deadline = time.monotonic() + 3
    while _live_process(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _live_process(child_pid)


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
    spec = ServerSpec(
        RunCommand(args=[sys.executable, "-c", script, str(marker)], cwd=tmp_path), str(tmp_path), lambda: None
    )

    with served_app(spec):
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
        while _live_process(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _live_process(pgid)
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


def _live_process(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False
