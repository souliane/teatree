"""The sweep ends what a Codex home started and spares PID 1's session, run on real processes."""

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
# Each program prints the pids of the `sleep` processes it leaves behind.
_TINI_LIKE = (
    "import subprocess, sys\n"
    "child = subprocess.Popen(['sleep', '600'])\n"
    "print(child.pid, flush=True)\n"
    "sys.exit(child.wait())\n"
)
_APP_SERVER_LIKE = (
    "import os, subprocess\n"
    "env = {key: value for key, value in os.environ.items() if key != 'CODEX_HOME'}\n"
    "same = subprocess.Popen(['sleep', '600'], env=env)\n"
    "own = subprocess.Popen(['sleep', '600'], env=env, start_new_session=True)\n"
    "orphan = subprocess.run(\n"
    "    ['sh', '-c', 'sleep 600 >/dev/null 2>&1 & echo $!'], env=env, capture_output=True, text=True\n"
    ")\n"
    "print(same.pid, own.pid, orphan.stdout.strip(), flush=True)\n"
    "same.wait()\n"
)


class _Tree:
    """Real processes in their own sessions, seen through a /proc that lists only them."""

    def __init__(self, root: Path) -> None:
        self.proc = root / "proc"
        self.proc.mkdir()
        self.spawned: list[subprocess.Popen[str]] = []

    def spawn(self, program: str, *, codex_home: str = "", as_init: bool = False) -> tuple[int, list[int]]:
        env = {"PATH": os.environ.get("PATH", "")} | ({"CODEX_HOME": codex_home} if codex_home else {})
        process = subprocess.Popen(
            [sys.executable, "-c", program], stdout=subprocess.PIPE, text=True, env=env, start_new_session=True
        )
        self.spawned.append(process)
        assert process.stdout is not None
        children = [int(pid) for pid in process.stdout.readline().split()]
        (self.proc / ("1" if as_init else str(process.pid))).symlink_to(f"/proc/{process.pid}")
        for child in children:
            (self.proc / str(child)).symlink_to(f"/proc/{child}")
        return process.pid, children

    def bystander(self) -> int:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
        self.spawned.append(process)
        (self.proc / str(process.pid)).symlink_to(f"/proc/{process.pid}")
        return process.pid

    def close(self) -> None:
        for pid in (int(entry.name) for entry in self.proc.iterdir() if entry.name != "1"):
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        for process in self.spawned:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            if process.stdout is not None:
                process.stdout.close()


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in {"Z", "X"}


@pytest.fixture
def tree(tmp_path: Path) -> Iterator[_Tree]:
    processes = _Tree(tmp_path)
    yield processes
    processes.close()


def test_sweep_ends_a_codex_session_and_spares_the_keeper_and_a_bystander(tree: _Tree) -> None:
    _, (keeper_sleep,) = tree.spawn(_TINI_LIKE, as_init=True)
    server, (same_session, own_session, orphan) = tree.spawn(
        _APP_SERVER_LIKE, codex_home=f"{EXECUTOR_HOME_ROOT}/codex-stale"
    )
    probe = tree.bystander()

    assert sweep(_REQUEST, tree.proc) == {"survivors": []}

    assert [_alive(pid) for pid in (server, same_session, own_session, orphan)] == [False] * 4
    assert _alive(keeper_sleep)
    assert _alive(probe)


def test_sweep_never_ends_pid_1s_session_even_when_it_carries_a_codex_home(tree: _Tree) -> None:
    keeper, (keeper_sleep,) = tree.spawn(_TINI_LIKE, codex_home=f"{EXECUTOR_HOME_ROOT}/codex-keeper", as_init=True)

    assert sweep(_REQUEST, tree.proc) == {"survivors": []}

    assert _alive(keeper)
    assert _alive(keeper_sleep)


def test_sweep_ignores_a_codex_home_outside_the_executor_root(tree: _Tree) -> None:
    tree.spawn(_TINI_LIKE, as_init=True)
    server, _ = tree.spawn(_APP_SERVER_LIKE, codex_home="/worker/.codex")

    assert sweep(_REQUEST, tree.proc) == {"survivors": []}

    assert _alive(server)


def test_sweep_reports_a_process_that_outlives_the_kill(tree: _Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    tree.spawn(_TINI_LIKE, as_init=True)
    server, children = tree.spawn(_APP_SERVER_LIKE, codex_home=f"{EXECUTOR_HOME_ROOT}/codex-stale")
    monkeypatch.setattr(codex_read_only_sweep_program.os, "kill", lambda pid, signal_number: None)

    assert sweep(_REQUEST, tree.proc) == {"survivors": sorted([server, *children])}


def test_sweep_refuses_without_a_readable_pid_1(tree: _Tree) -> None:
    tree.spawn(_APP_SERVER_LIKE, codex_home=f"{EXECUTOR_HOME_ROOT}/codex-stale")

    with pytest.raises(codex_read_only_sweep_program.SweepError, match="PID 1"):
        sweep(_REQUEST, tree.proc)
