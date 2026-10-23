import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("arguments", [[], ["--full"]])
def test_full_affected_lane_runs_all_real_shards(tmp_path: Path, arguments: list[str]) -> None:
    root = Path(__file__).resolve().parents[1]
    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "uv-calls"
    uv = tools / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$UV_CALLS"\n')
    uv.chmod(0o700)
    t3 = tools / "t3"
    t3.write_text(
        '#!/bin/sh\ncase " $* " in *" --pytest-args "*) echo "--teatree-selected-full"; exit 0;; esac\n'
        'echo "affected-tests: FULL"\n'
    )
    t3.chmod(0o700)
    result = subprocess.run(
        ["/bin/bash", "dev/test-affected.sh", *arguments],
        cwd=root,
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "UV_CALLS": str(calls)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    assert len(lines) == 12
    assert all(f"--splits 12 --group {group}" in line for group, line in enumerate(lines, start=1))
