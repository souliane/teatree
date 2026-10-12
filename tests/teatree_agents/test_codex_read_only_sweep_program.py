"""The sweep ends every process outside PID 1's session but the worker's own exec programs, on real processes."""

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from teatree.agents import codex_read_only_sweep_program
from teatree.agents.codex_read_only_sweep_program import SweepRequest, sweep
from teatree.agents.codex_read_only_verdict import EXECUTOR_HOME_ROOT

_REQUEST = SweepRequest(home_root=EXECUTOR_HOME_ROOT)
_SESSION_HOME = f"{EXECUTOR_HOME_ROOT}/codex-stale"

# A pid namespace's init: orphans reparent to it (PR_SET_CHILD_SUBREAPER), it keeps a `sleep` in its own
# session like tini, and its shim child plays runc, whose exec roots have a parent outside the namespace.
_CONTAINER_INIT = (
    "import ctypes, os, subprocess, sys, time\n"
    "if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):\n"
    "    sys.exit('prctl(PR_SET_CHILD_SUBREAPER) failed')\n"
    "keeper = subprocess.Popen(['sleep', '600'])\n"
    "shim = subprocess.Popen([sys.executable, '-c', sys.argv[1]])\n"
    "print(keeper.pid, shim.pid, flush=True)\n"
    "while True:\n"
    "    try:\n"
    "        os.wait()\n"
    "    except ChildProcessError:\n"
    "        time.sleep(0.05)\n"
)
_SHIM = (
    "import json, subprocess, sys\n"
    "for line in sys.stdin:\n"
    "    request = json.loads(line)\n"
    "    root = subprocess.Popen([sys.executable, '-c', request['program']], env=request['env'],\n"
    "                            stdout=subprocess.PIPE, text=True, start_new_session=True)\n"
    "    print(json.dumps([root.pid, *map(int, root.stdout.readline().split())]), flush=True)\n"
)
# Each exec program prints the pids it leaves behind, then stays alive.
_CODEX_SESSION = (
    "import os, subprocess, time\n"
    "env = {key: value for key, value in os.environ.items() if key != 'CODEX_HOME'}\n"
    "same = subprocess.Popen(['sleep', '600'], env=env)\n"
    "own = subprocess.Popen(['sleep', '600'], env=env, start_new_session=True)\n"
    "def detached(command):\n"
    "    return subprocess.run(['sh', '-c', command], env=env, capture_output=True, text=True).stdout.strip()\n"
    "orphan = detached('sleep 600 >/dev/null 2>&1 & echo $!')\n"
    "escaped = detached('(env -i setsid sleep 600 >/dev/null 2>&1 & echo $!)')\n"
    "print(same.pid, own.pid, orphan, escaped, flush=True)\n"
    "time.sleep(600)\n"
)
_WORKER_PROGRAM = "import time\nprint(flush=True)\ntime.sleep(600)\n"
_UNDUMPABLE_PROGRAM = (
    "import ctypes, time\nctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)\nprint(flush=True)\ntime.sleep(600)\n"
)


def _stat(pid: int) -> tuple[str, int] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return fields[0], int(fields[1])


def _alive(pid: int) -> bool:
    stat = _stat(pid)
    return stat is not None and stat[0] not in {"Z", "X"}


class _Container:
    """Real processes under a subreaper init, seen through a /proc that lists the init's tree but the shim."""

    def __init__(self, root: Path, *, init_codex_home: str = "") -> None:
        self.root = root
        env = {"PATH": os.environ.get("PATH", "")} | ({"CODEX_HOME": init_codex_home} if init_codex_home else {})
        self.init = subprocess.Popen(
            [sys.executable, "-c", _CONTAINER_INIT, _SHIM],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        assert self.init.stdout is not None
        self.keeper, self.shim = map(int, self.init.stdout.readline().split())

    def exec(self, program: str, *, codex_home: str = "") -> list[int]:
        """``docker exec`` of *program*: the root pid, then the pids it printed."""
        env = {"PATH": os.environ.get("PATH", "")} | ({"CODEX_HOME": codex_home} if codex_home else {})
        assert self.init.stdin is not None
        assert self.init.stdout is not None
        self.init.stdin.write(json.dumps({"program": program, "env": env}) + "\n")
        self.init.stdin.flush()
        pids: list[int] = json.loads(self.init.stdout.readline())
        return pids

    def members(self) -> list[int]:
        parents = {pid: stat[1] for pid in _host_pids() if (stat := _stat(pid)) is not None}
        return sorted(pid for pid in parents if pid not in {self.init.pid, self.shim} and self._inside(pid, parents))

    def _inside(self, pid: int, parents: dict[int, int]) -> bool:
        seen: set[int] = set()
        while pid in parents and pid not in seen:
            seen.add(pid)
            if pid == self.init.pid:
                return True
            pid = parents[pid]
        return False

    def proc(self) -> Path:
        """The container's /proc as the sweep reads it: the init as ``1``, every other member under its pid."""
        proc = self.root / "proc"
        if proc.exists():
            for entry in proc.iterdir():
                entry.unlink()
        proc.mkdir(exist_ok=True)
        (proc / "1").symlink_to(f"/proc/{self.init.pid}")
        for pid in self.members():
            (proc / str(pid)).symlink_to(f"/proc/{pid}")
        return proc

    def close(self) -> None:
        for pid in self.members():
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        with suppress(ProcessLookupError):
            os.killpg(self.init.pid, signal.SIGKILL)
        self.init.wait()
        for stream in (self.init.stdin, self.init.stdout):
            if stream is not None:
                stream.close()


def _host_pids() -> list[int]:
    return [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit()]


@pytest.fixture
def container(tmp_path: Path) -> Iterator[_Container]:
    processes = _Container(tmp_path)
    yield processes
    processes.close()


def test_a_detached_env_cleared_process_from_a_codex_session_is_ended(container: _Container) -> None:
    server, same_session, own_session, orphan, escaped = container.exec(_CODEX_SESSION, codex_home=_SESSION_HOME)
    assert (_stat(escaped) or ("", 0))[1] == container.init.pid

    assert sweep(_REQUEST, container.proc()) == {"survivors": []}

    assert [_alive(pid) for pid in (server, same_session, own_session, orphan, escaped)] == [False] * 5
    assert _alive(container.keeper)
    assert _alive(container.init.pid)


def test_an_orphan_of_a_dead_session_is_ended_without_its_codex_home(container: _Container) -> None:
    server, *_, escaped = container.exec(_CODEX_SESSION, codex_home=_SESSION_HOME)
    os.kill(server, signal.SIGKILL)

    assert sweep(_REQUEST, container.proc()) == {"survivors": []}

    assert not _alive(escaped)


def test_the_workers_own_exec_programs_are_spared(container: _Container) -> None:
    (probe,) = container.exec(_WORKER_PROGRAM)
    (elsewhere,) = container.exec(_WORKER_PROGRAM, codex_home="/worker/.codex")

    assert sweep(_REQUEST, container.proc()) == {"survivors": []}

    assert _alive(probe)
    assert _alive(elsewhere)


def test_an_exec_program_whose_environment_cannot_be_read_is_ended(container: _Container) -> None:
    (unreadable,) = container.exec(_UNDUMPABLE_PROGRAM)

    assert sweep(_REQUEST, container.proc()) == {"survivors": []}

    assert not _alive(unreadable)


def test_pid_1s_session_survives_even_when_it_carries_a_codex_home(tmp_path: Path) -> None:
    container = _Container(tmp_path, init_codex_home=f"{EXECUTOR_HOME_ROOT}/codex-keeper")
    try:
        assert sweep(_REQUEST, container.proc()) == {"survivors": []}

        assert _alive(container.init.pid)
        assert _alive(container.keeper)
    finally:
        container.close()


def test_a_process_that_outlives_the_kill_is_reported(container: _Container, monkeypatch: pytest.MonkeyPatch) -> None:
    pids = container.exec(_CODEX_SESSION, codex_home=_SESSION_HOME)
    (probe,) = container.exec(_WORKER_PROGRAM)
    monkeypatch.setattr(codex_read_only_sweep_program.os, "kill", lambda pid, signal_number: None)

    survivors = sweep(_REQUEST, container.proc())["survivors"]

    assert survivors == sorted(survivors)
    assert set(pids) <= set(survivors)
    assert probe not in survivors


def test_sweep_refuses_without_a_readable_pid_1(tmp_path: Path) -> None:
    (tmp_path / "proc").mkdir()

    with pytest.raises(codex_read_only_sweep_program.SweepError, match="PID 1"):
        sweep(_REQUEST, tmp_path / "proc")
