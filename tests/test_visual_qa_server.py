"""The checkout server is isolated and tears down its whole process group."""

import os
import sqlite3
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
        "os.environ.get('T3_CONFIG_DB','absent'),flush=True);"
        "s=socket.socket();s.bind(('127.0.0.1',int(os.environ['T3_VISUAL_QA_PORT'])));"
        "s.listen();time.sleep(30)"
    )
    spec = ServerSpec(RunCommand(args=[sys.executable, "-c", script], cwd=tmp_path), str(tmp_path), lambda: None)

    with served_app(spec) as app:
        assert "absent absent" in app.log_path.read_text()

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


def _live_process(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False
