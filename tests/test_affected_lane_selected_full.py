"""``dev/test-affected.sh`` declares a FULL verdict to the whole-tree guard, never a scoped one (#5110)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.quality.affected_tests import Selection

_BASH = shutil.which("bash") or "/bin/bash"
_LANE = Path(__file__).resolve().parents[1] / "dev" / "test-affected.sh"
_SCOPED = "--tach --tach-base origin/main -p teatree.quality.force_keep_plugin"


def _lane_result(
    tmp_path: Path, *lane_args: str, selection: str, selector_exit: int = 0
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "t3": (f'if [[ " $* " == *" --pytest-args "* ]]; then echo "{selection}"; exit {selector_exit}; fi\nexit 0'),
        "uv": 'printf "%s\\n" "$@"',
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(f"#!/usr/bin/env bash\n{body}\n")
        (bin_dir / name).chmod(0o755)
    return subprocess.run(
        [_BASH, str(_LANE), *lane_args],
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _pytest_argv(tmp_path: Path, *lane_args: str, selection: str) -> list[str]:
    result = _lane_result(tmp_path, *lane_args, selection=selection)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


@pytest.mark.parametrize(
    ("lane_args", "selection"),
    [(("--full",), ""), ((), " ".join(Selection(full=True, reason="migration", create_db=True).pytest_args()))],
)
def test_a_full_verdict_declares_itself_to_the_whole_tree_guard(
    tmp_path: Path, lane_args: tuple[str, ...], selection: str
) -> None:
    assert "--teatree-selected-full" in _pytest_argv(tmp_path, *lane_args, selection=selection)


def test_a_full_verdict_without_a_database_change_runs_the_whole_tree(tmp_path: Path) -> None:
    selection = " ".join(Selection(full=True, reason="changed selector").pytest_args())
    assert "--teatree-selected-full" in _pytest_argv(tmp_path, selection=selection)


def test_a_scoped_selection_stays_under_the_whole_tree_guard(tmp_path: Path) -> None:
    argv = _pytest_argv(tmp_path, selection=_SCOPED)
    assert "--tach" in argv
    assert "--teatree-selected-full" not in argv


@pytest.mark.parametrize(("selection", "selector_exit"), [("", 0), ("--tach", 7)])
def test_a_failed_or_empty_selector_refuses_to_run_pytest(tmp_path: Path, selection: str, selector_exit: int) -> None:
    result = _lane_result(tmp_path, selection=selection, selector_exit=selector_exit)
    assert result.returncode == 4
    assert "refusing to run pytest" in result.stderr
    assert "--teatree-selected-full" not in result.stdout
