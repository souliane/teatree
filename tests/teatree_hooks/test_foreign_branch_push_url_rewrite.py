# test-path: cross-cutting — drives hooks/scripts/foreign_branch_push_git.py with real Git repositories.
"""Push probes resolve the checkout's destination before leaving it."""

import shutil
import subprocess
from pathlib import Path

import pytest

from hooks.scripts.foreign_branch_push_git import remote_branch_oid


def _git(work: Path, *args: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    result = subprocess.run([executable, "-C", str(work), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def rewrite_scene(tmp_path: Path) -> tuple[Path, str, str]:
    work, fetch, actual, alias = (tmp_path / name for name in ("work", "fetch.git", "actual.git", "alias.git"))
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    _git(tmp_path, "init", "-q", "--bare", str(fetch))
    _git(tmp_path, "init", "-q", "--bare", str(actual))
    (work / "seed").write_text("seed", encoding="utf-8")
    _git(work, "add", "seed")
    _git(work, "-c", "user.name=Example", "-c", "user.email=example@example.test", "commit", "-qm", "seed")
    _git(work, "branch", "feature")
    _git(work, "push", "-q", str(actual), "feature")
    _git(work, "remote", "add", "origin", fetch.as_uri())
    _git(work, "remote", "set-url", "--push", "origin", alias.as_uri())
    _git(work, "config", "--local", f"url.{actual.as_uri()}.insteadOf", alias.as_uri())
    return work, alias.as_uri(), _git(work, "rev-parse", "feature")


@pytest.mark.parametrize("remote_kind", ["origin", "url"])
def test_remote_branch_probe_finds_checkout_push_destination(
    rewrite_scene: tuple[Path, str, str], remote_kind: str
) -> None:
    work, alias, oid = rewrite_scene
    remote = "origin" if remote_kind == "origin" else alias
    checkout_ref = _git(work, "ls-remote", "--heads", alias, "refs/heads/feature")

    probe = remote_branch_oid(str(work), remote, "feature")

    assert oid in checkout_ref
    assert probe.ok, probe.err
    assert probe.out == oid
