import os
import shutil
import subprocess
from pathlib import Path


def test_docker_matrix_explicitly_selects_full_suite(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    calls = tmp_path / "docker-calls"
    docker = binary_dir / "docker"
    docker.write_text(
        '#!/bin/sh\nif [ "$1" = "image" ]; then exit 0; fi\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS"\necho "1 passed"\n'
    )
    docker.chmod(0o700)
    bash = shutil.which("bash")
    git = shutil.which("git")
    assert bash
    assert git

    result = subprocess.run(
        [bash, "dev/test-matrix.sh"],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{binary_dir}:{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "DOCKER_CALLS": str(calls),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    runs = calls.read_text().splitlines()
    assert len(runs) == 2
    assert all("--teatree-selected-full" in run for run in runs)
    git_common_dir = subprocess.check_output(
        [git, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo,
        text=True,
    ).strip()
    assert all(f"-v {git_common_dir}:{git_common_dir}:ro" in run for run in runs)


def test_docker_matrix_runs_without_git_metadata(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    archive = tmp_path / "archive"
    (archive / "dev").mkdir(parents=True)
    shutil.copy2(repo / "dev/test-matrix.sh", archive / "dev/test-matrix.sh")
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    calls = tmp_path / "docker-calls"
    docker = binary_dir / "docker"
    docker.write_text(
        '#!/bin/sh\nif [ "$1" = "image" ]; then exit 0; fi\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS"\necho "1 passed"\n'
    )
    docker.chmod(0o700)
    bash = shutil.which("bash")
    assert bash

    result = subprocess.run(
        [bash, "dev/test-matrix.sh", "3.13"],
        cwd=archive,
        env={
            **os.environ,
            "PATH": f"{binary_dir}:{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "DOCKER_CALLS": str(calls),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--teatree-selected-full" in calls.read_text()
