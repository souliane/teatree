import fcntl
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.utils import push_gate_lock
from tests._push_gate_lane import install_uv_stub, lane_repo, run_lane, stage_runs

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCK_MODULE = _REPO_ROOT / "src" / "teatree" / "utils" / "push_gate_lock.py"
_LOCK_HELPER = _REPO_ROOT / "dev" / "lib" / "gate-lock.sh"
_PUSH_GATE = _REPO_ROOT / "dev" / "push-gate.sh"
_BASH = shutil.which("bash") or "/bin/bash"


def _wait_until_holder_recorded(lock: Path) -> None:
    deadline = time.monotonic() + 30
    while "pid=" not in (lock.read_text(encoding="utf-8") if lock.exists() else ""):
        assert time.monotonic() < deadline, f"nobody took {lock}"
        time.sleep(0.02)


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

    def test_unopenable_lock_aborts_before_any_check(self, tmp_path: Path) -> None:
        lock = tmp_path / "absent" / "push-gate.lock"
        result = _run_bash(f'source "{_LOCK_HELPER}"; T3_PUSH_GATE_LOCK="{lock}" acquire_push_gate_lock; echo "rc=$?"')

        assert result.stdout.strip() == "rc=75", result.stderr
        assert f"=== push-gate: ABORTED: cannot open lock {lock}" in result.stderr
        assert "unserialised" not in result.stderr

    @pytest.mark.parametrize(
        ("helper", "helper_rc"),
        [
            ("export T3_PUSH_GATE_LOCK_WAIT_SECONDS=not-a-number", 2),
            ("python3() { sh -c 'kill -TERM $$'; }", 143),
        ],
        ids=["bad-wait-argument", "killed-by-sigterm"],
    )
    def test_a_failing_helper_aborts_and_releases_the_lock(self, tmp_path: Path, helper: str, helper_rc: int) -> None:
        lock = tmp_path / "push-gate.lock"
        result = _run_bash(
            f'source "{_LOCK_HELPER}"; {helper}; export T3_PUSH_GATE_LOCK="{lock}"; '
            'acquire_push_gate_lock; rc=$?; fd9=open; { : >&9; } 2>/dev/null || fd9=closed; echo "rc=$rc fd9=$fd9"'
        )

        assert result.stdout.strip() == "rc=75 fd9=closed", result.stderr
        assert f"=== push-gate: ABORTED: lock helper failed (rc={helper_rc}) for {lock}" in result.stderr

    def test_a_timed_out_wait_aborts_once_naming_the_holder(self, tmp_path: Path) -> None:
        lock = tmp_path / "push-gate.lock"
        with lock.open("w+", encoding="utf-8") as holder:
            holder.write("pid=2468 repo=/holder/worktree\n")
            holder.flush()
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = _run_bash(
                f'source "{_LOCK_HELPER}"; export T3_PUSH_GATE_LOCK="{lock}" T3_PUSH_GATE_LOCK_WAIT_SECONDS=0; '
                'acquire_push_gate_lock; echo "rc=$?"'
            )

        assert result.stdout.strip() == "rc=75", result.stderr
        assert result.stderr.count("=== push-gate: ABORTED") == 1, result.stderr
        assert "pid=2468 repo=/holder/worktree" in result.stderr


@pytest.mark.integration
class TestThePushGateRunsNoIncrementalSweepWithoutTheLock:
    def test_an_unopenable_lock_runs_only_serial_checks(self, tmp_path: Path) -> None:
        install_uv_stub(tmp_path)
        lock = tmp_path / "absent" / "push-gate.lock"

        completed, stages, _ = run_lane(
            tmp_path, lane_repo(tmp_path, vendored=False), extra_env={"T3_PUSH_GATE_LOCK": str(lock)}
        )

        assert completed.returncode == 75, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert len(stages) == 2
        assert [run.lock for run in stage_runs(tmp_path)] == ["unavailable", "unavailable"]
        assert f"=== push-gate: ABORTED: cannot open lock {lock}" in completed.stderr

    def test_a_timed_out_wait_runs_only_serial_checks(self, tmp_path: Path) -> None:
        install_uv_stub(tmp_path)
        lock = tmp_path / "push-gate.lock"
        with lock.open("w+", encoding="utf-8") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            completed, stages, _ = run_lane(
                tmp_path, lane_repo(tmp_path, vendored=False), extra_env={"T3_PUSH_GATE_LOCK_WAIT_SECONDS": "0"}
            )

        assert completed.returncode == 75, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert len(stages) == 2
        assert [run.lock for run in stage_runs(tmp_path)] == ["held", "held"]

    def test_a_held_lock_runs_every_stage_and_names_this_run(self, tmp_path: Path) -> None:
        install_uv_stub(tmp_path)

        completed, stages, _ = run_lane(tmp_path, lane_repo(tmp_path, vendored=False))

        assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert len(stages) == 3
        assert "pid=" in (tmp_path / "push-gate.lock").read_text(encoding="utf-8")
        assert [run.lock for run in stage_runs(tmp_path)] == ["free", "free", "held"]

    def test_two_pushers_never_run_their_incremental_sweeps_at_once(self, tmp_path: Path) -> None:
        lock = tmp_path / "shared.lock"
        env = {"T3_PUSH_GATE_LOCK": str(lock), "T3_PUSH_GATE_LOCK_WAIT_SECONDS": "120", "UV_STUB_STAGE_SLEEP": "0.5"}
        first_lane, second_lane = tmp_path / "first", tmp_path / "second"
        for lane in (first_lane, second_lane):
            lane.mkdir()
            install_uv_stub(lane)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run_lane, first_lane, lane_repo(first_lane, vendored=False), extra_env=env)
            _wait_until_holder_recorded(lock)
            second = pool.submit(run_lane, second_lane, lane_repo(second_lane, vendored=False), extra_env=env)
            completed = [first.result()[0], second.result()[0]]

        assert [lane.returncode for lane in completed] == [0, 0], [lane.stderr for lane in completed]
        first_runs, second_runs = stage_runs(first_lane), stage_runs(second_lane)
        assert len(first_runs) == len(second_runs) == 3
        assert first_runs[2].ended <= second_runs[2].started


def test_push_gate_acquires_before_sizing_the_worker_pool() -> None:
    body = _PUSH_GATE.read_text(encoding="utf-8")

    assert body.index("acquire_push_gate_lock") < body.index("bound_xdist_workers_to_memory")


def test_push_gate_sizes_the_worker_pool_just_before_the_locked_stage() -> None:
    body = _PUSH_GATE.read_text(encoding="utf-8")

    assert body.index("bound_xdist_workers_to_memory") < body.index("record_push_gate_bound")
    assert body.index("record_push_gate_bound") < body.index("record_push_gate_stage 3")
    assert body.index("record_push_gate_stage 2") < body.index("bound_xdist_workers_to_memory")


def test_lock_starts_after_the_two_serial_safety_stages() -> None:
    body = _PUSH_GATE.read_text(encoding="utf-8")

    assert body.index("record_push_gate_stage 2") < body.index("acquire_push_gate_lock")
    assert body.index("acquire_push_gate_lock") < body.index("record_push_gate_stage 3")


def test_push_gate_closes_the_lock_for_every_stage() -> None:
    body = _PUSH_GATE.read_text(encoding="utf-8")

    stage_lines = [
        line
        for line in body.splitlines()
        if line.startswith(
            (
                '"${UV_PROJECT_RUN[@]}" run pytest',
                "timeout --kill-after",
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


class TestLockAbortSaysWhatDidNotRun:
    def test_an_unopenable_lock_reports_that_stage_three_did_not_run(self, tmp_path: Path) -> None:
        result = _run_bash(
            f'. dev/lib/gate-lock.sh; T3_PUSH_GATE_LOCK="{tmp_path}/missing-dir/push-gate.lock" acquire_push_gate_lock'
        )

        assert result.returncode == 75
        assert "stage 3 (the incremental sweep) did not run" in result.stderr
        assert "no check ran" not in result.stderr

    def test_a_failing_lock_helper_reports_that_stage_three_did_not_run(self, tmp_path: Path) -> None:
        result = _run_bash(
            f'. dev/lib/gate-lock.sh; T3_PUSH_GATE_LOCK="{tmp_path}/push-gate.lock" '
            "T3_PUSH_GATE_LOCK_WAIT_SECONDS=not-a-number acquire_push_gate_lock"
        )

        assert result.returncode == 75
        assert "lock helper failed" in result.stderr
        assert "stage 3 (the incremental sweep) did not run" in result.stderr
        assert "no check ran" not in result.stderr
