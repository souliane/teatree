"""``dev/test-affected.sh`` declares a FULL verdict to the whole-tree guard, never a scoped one (#5110)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_BASH = shutil.which("bash") or "/bin/bash"
_LANE = Path(__file__).resolve().parents[1] / "dev" / "test-affected.sh"
_SCOPED = "--tach --tach-base origin/main -p teatree.quality.force_keep_plugin"


def _pytest_argv(tmp_path: Path, *lane_args: str, selection: str) -> list[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "t3": f'[[ " $* " == *" --pytest-args "* ]] && echo "{selection}"\nexit 0',
        "uv": 'printf "%s\\n" "$@"',
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(f"#!/usr/bin/env bash\n{body}\n")
        (bin_dir / name).chmod(0o755)
    result = subprocess.run(
        [_BASH, str(_LANE), *lane_args],
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.splitlines()


@pytest.mark.parametrize(("lane_args", "selection"), [(("--full",), ""), ((), ""), ((), "--create-db")])
def test_a_full_verdict_declares_itself_to_the_whole_tree_guard(
    tmp_path: Path, lane_args: tuple[str, ...], selection: str
) -> None:
    assert "--teatree-selected-full" in _pytest_argv(tmp_path, *lane_args, selection=selection)


def test_a_scoped_selection_stays_under_the_whole_tree_guard(tmp_path: Path) -> None:
    argv = _pytest_argv(tmp_path, selection=_SCOPED)
    assert "--tach" in argv
    assert "--teatree-selected-full" not in argv
