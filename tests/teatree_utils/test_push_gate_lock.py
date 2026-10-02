import fcntl
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from teatree.utils import push_gate_lock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCK_MODULE = _REPO_ROOT / "src" / "teatree" / "utils" / "push_gate_lock.py"
_LOCK_HELPER = _REPO_ROOT / "dev" / "lib" / "gate-lock.sh"
_PUSH_GATE = _REPO_ROOT / "dev" / "push-gate.sh"
_BASH = shutil.which("bash") or "/bin/bash"


def _run_bash(script: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, "-c", script],
        cwd=cwd or _REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class TestPushGateLock:
    def test_acquisition_writes_the_holder_record(self, tmp_path: Path) -> None:
        lock = tmp_path / "push-gate.lock"
        result = _run_bash(f'exec 9<>"{lock}"; T3_PUSH_GATE_HOLDER_PID=4321 "{sys.executable}" "{_LOCK_MODULE}" 0')

        assert result.returncode == 0, result.stderr
        record = lock.read_text(encoding="utf-8")
        assert "pid=4321" in record
        assert f"repo={_REPO_ROOT}" in record

    def test_contention_names_the_holder_then_aborts_with_exit_75(self, tmp_path: Path) -> None:
        lock = tmp_path / "push-gate.lock"
        with lock.open("w+", encoding="utf-8") as holder:
            holder.write("pid=2468 repo=/holder/worktree\n")
            holder.flush()
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = _run_bash(f'exec 9<>"{lock}"; "{sys.executable}" "{_LOCK_MODULE}" 0')

        assert result.returncode == 75
        assert "pid=2468 repo=/holder/worktree" in result.stderr
        assert "elapsed=0s" in result.stderr
        assert "max=0s" in result.stderr
        assert "ABORTED" in result.stderr

    def test_contention_repeats_the_heartbeat_every_30_seconds(self, tmp_path: Path) -> None:
        lock = tmp_path / "push-gate.lock"
        with (
            lock.open("w+b") as handle,
            patch.object(
                push_gate_lock.fcntl,
                "flock",
                side_effect=[BlockingIOError, BlockingIOError, None],
            ),
            patch.object(push_gate_lock.time, "monotonic", side_effect=[0.0, 0.0, 31.0]),
            patch.object(push_gate_lock.time, "sleep"),
            patch.object(push_gate_lock, "_emit") as emit,
        ):
            result = push_gate_lock.acquire_push_gate_lock(60, fd=handle.fileno())

        assert result == 0
        heartbeats = [call.args[0] for call in emit.call_args_list if "waiting for lock" in call.args[0]]
        assert len(heartbeats) == 2
        assert "elapsed=0s max=60s" in heartbeats[0]
        assert "elapsed=31s max=60s" in heartbeats[1]

    def test_unopenable_lock_warns_and_runs_unserialised(self, tmp_path: Path) -> None:
        lock = tmp_path / "absent" / "push-gate.lock"
        result = _run_bash(
            f'source "{_LOCK_HELPER}"; T3_PUSH_GATE_LOCK="{lock}" acquire_push_gate_lock; '
            "rc=$?; echo continued; exit $rc"
        )

        assert result.returncode == 0
        assert "WARNING" in result.stderr
        assert "unserialised" in result.stderr
        assert result.stdout.strip() == "continued"


def test_push_gate_acquires_before_sizing_the_worker_pool() -> None:
    body = _PUSH_GATE.read_text(encoding="utf-8")

    assert body.index("acquire_push_gate_lock") < body.index("bound_xdist_workers_to_memory")


def test_push_gate_closes_the_lock_for_every_stage() -> None:
    body = _PUSH_GATE.read_text(encoding="utf-8")

    stage_lines = [
        line
        for line in body.splitlines()
        if line.startswith(
            (
                '"${UV_PROJECT_RUN[@]}" run pytest',
                '"$timeout_bin" --kill-after',
                '"${UV_PROJECT_RUN[@]}" run t3 tool push-gate --run',
            )
        )
    ]
    assert len(stage_lines) == 3
    assert all("9>&-" in line for line in stage_lines), stage_lines


def test_lock_helper_keeps_the_portable_path_fallbacks() -> None:
    body = _LOCK_HELPER.read_text(encoding="utf-8")

    assert "T3_PUSH_GATE_LOCK" in body
    assert "/host-tmp/t3-push-gate.lock" in body
    assert "/tmp/t3-push-gate.lock" in body
    assert 'exec 9<>"$lock_path"' in body
