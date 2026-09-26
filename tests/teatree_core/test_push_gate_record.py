"""The push-gate diagnostic belongs to one worktree and survives a hard kill."""

import shutil
import subprocess
import time
from pathlib import Path

from teatree.core.push_gate_record import GateRunRecord
from tests._git_repo import make_git_repo, run_git

_BASH = shutil.which("bash") or "/bin/bash"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELPER = _REPO_ROOT / "dev" / "lib" / "gate-record.sh"
_LOCK_HELPER = _REPO_ROOT / "dev" / "lib" / "gate-lock.sh"
_PUSH_GATE = _REPO_ROOT / "dev" / "push-gate.sh"


def _record_path(repo: Path) -> Path:
    value = run_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "t3-push-gate-run")
    return Path(value.strip())


def test_shell_record_captures_the_gate_run_in_the_worktree_git_dir(tmp_path: Path) -> None:
    main = make_git_repo(tmp_path / "main")
    worktree = tmp_path / "worktree"
    run_git(main, "worktree", "add", "-q", "-b", "feature", str(worktree))
    script = f"""
set -euo pipefail
. "{_HELPER}"
export T3_PUSH_GATE_LOCK_PATH=/tmp/push.lock
export T3_PUSH_GATE_LOCK_WAIT_S=7
export T3_XDIST_BOUND_SUMMARY='workers=2 cap_mib=6144 headroom_mib=2048 reserve_mib=512 per_worker_mib=512'
start_push_gate_record
record_push_gate_lock
record_push_gate_bound
record_push_gate_stage 3
finish_push_gate_record 1
"""

    subprocess.run([_BASH, "-c", script], cwd=worktree, check=True)

    record_path = _record_path(worktree)
    assert record_path.is_file()
    common_dir = Path(run_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    assert record_path != common_dir / record_path.name
    record = GateRunRecord.read(worktree, since=0)
    assert record is not None
    assert record.lock == "/tmp/push.lock"
    assert record.lock_wait_s == 7
    assert record.bound.startswith("workers=2 cap_mib=6144")
    assert record.stage == 3
    assert record.rc == 1
    assert not record.died_mid_run


def test_reader_ignores_a_record_older_than_the_push(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    _record_path(repo).write_text("started=10\npid=12\nstage=2\n", encoding="utf-8")

    assert GateRunRecord.read(repo, since=11) is None


def test_unfinished_record_identifies_a_gate_that_died_mid_run(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    started = int(time.time())
    _record_path(repo).write_text(f"started={started}\npid=12\nstage=2\n", encoding="utf-8")

    record = GateRunRecord.read(repo, since=started)

    assert record is not None
    assert record.died_mid_run
    assert "stage=2" in record.summary


def test_lock_helper_exports_the_selected_path_and_wait(tmp_path: Path) -> None:
    lock = tmp_path / "push-gate.lock"
    script = f"""
set -euo pipefail
python3() {{ return 0; }}
. "{_LOCK_HELPER}"
export T3_PUSH_GATE_LOCK="{lock}"
acquire_push_gate_lock
printf '%s\n%s\n' "$T3_PUSH_GATE_LOCK_PATH" "$T3_PUSH_GATE_LOCK_WAIT_S"
"""

    completed = subprocess.run([_BASH, "-c", script], capture_output=True, text=True, check=True)

    assert completed.stdout.splitlines() == [str(lock), "0"]


def test_push_gate_records_every_stage_and_finishes_via_exit_trap() -> None:
    body = _PUSH_GATE.read_text(encoding="utf-8")

    assert body.index("start_push_gate_record") < body.index("acquire_push_gate_lock")
    assert "trap 'finish_push_gate_record \"$?\"' EXIT" in body
    stage_commands = (
        '"${UV_PROJECT_RUN[@]}" run pytest tests/test_gate_never_lockout_contract.py',
        '"$timeout_bin" --kill-after=10s',
        '"${UV_PROJECT_RUN[@]}" run t3 tool push-gate --run',
    )
    for stage, command in enumerate(stage_commands, start=1):
        assert body.index(f"record_push_gate_stage {stage}") < body.index(command)
