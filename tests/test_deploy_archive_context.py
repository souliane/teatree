# test-path: cross-cutting — drives deploy/deploy.sh's build context staging (no src mirror).
"""The shipped archive step stages a Dockerfile from both supported Git layouts."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._git_repo import make_git_repo

DEPLOY_SH = Path(__file__).resolve().parents[1] / "deploy" / "deploy.sh"
GIT = shutil.which("git") or "/usr/bin/git"
BASH = shutil.which("bash") or "/bin/bash"
GIT_ENV = {
    **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",  # privacy-scan:allow
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",  # privacy-scan:allow
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(repo: Path, *args: str, input_data: str | None = None) -> str:
    return subprocess.run(
        [GIT, "-C", str(repo), *args],
        input=input_data,
        text=True,
        capture_output=True,
        check=True,
        env=GIT_ENV,
    ).stdout.strip()


def _archive_step() -> str:
    source = DEPLOY_SH.read_text(encoding="utf-8")
    start = source.index('BUILD_CONTEXT="$(mktemp -d ')
    end = source.index('echo "deploy: deploying ', start)
    return source[start:end]


def _stage(repo_root: Path, commit: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    script = (
        "set -euo pipefail\n"
        f"REPO_ROOT={shlex.quote(str(repo_root))}\n"
        f"DEPLOY_COMMIT={shlex.quote(commit)}\n" + _archive_step()
    )
    result = subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={**GIT_ENV, "TMPDIR": str(build_dir)},
    )
    contexts = list(build_dir.glob("teatree-build.*"))
    assert len(contexts) == 1
    return result, contexts[0]


@pytest.mark.parametrize("prefix", ["", "vendor/teatree"])
def test_archive_contains_dockerfile_in_both_layouts(tmp_path: Path, prefix: str) -> None:
    repo = make_git_repo(tmp_path / "repo", initial_commit=False)
    core = repo / prefix
    dockerfile = core / "deploy" / "Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")

    result, context = _stage(core, _git(repo, "rev-parse", "HEAD"), tmp_path)

    assert result.returncode == 0, result.stderr
    assert (context / "deploy" / "Dockerfile").read_text(encoding="utf-8") == "FROM scratch\n"


def test_empty_archive_fails_with_archive_path_and_prefix(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo", initial_commit=False)
    empty_tree = _git(repo, "mktree", input_data="")

    result, _ = _stage(repo, empty_tree, tmp_path)

    assert result.returncode != 0
    assert f"archive {empty_tree}:" in result.stderr
    assert "prefix ''" in result.stderr
    assert "deploy/Dockerfile" in result.stderr
    assert len(result.stderr.splitlines()) == 1
