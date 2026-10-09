# test-path: cross-cutting
"""The ``no-commit-on-merged-branch`` hook refuses exactly a branch whose upstream is gone.

``git branch -vv`` renders a deleted upstream as ``[origin/<b>: gone]``, never a bare
``[gone]``, and it prints the HEAD commit's subject on the same line — so a grep for
``[gone]`` over that output reads the subject, not the tracking state. The hook runs
from teatree's own config and from the overlay template, so both are exercised.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIGS = (
    _REPO_ROOT / ".pre-commit-config.yaml",
    _REPO_ROOT / "src" / "teatree" / "templates" / "overlay" / ".pre-commit-config.yaml.tmpl",
)
_GIT = shutil.which("git") or "/usr/bin/git"
_HOOK_ID = "no-commit-on-merged-branch"


def _clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run([_GIT, "-C", str(cwd), *args], check=True, capture_output=True, env=_clean_env())


def _hook_entry(config: Path) -> str:
    loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
    hooks = [hook for repo in loaded["repos"] for hook in repo.get("hooks", [])]
    return next(hook["entry"] for hook in hooks if hook["id"] == _HOOK_ID)


def _hook_refuses(work: Path, config: Path) -> bool:
    result = subprocess.run(
        shlex.split(_hook_entry(config)), cwd=work, capture_output=True, text=True, env=_clean_env(), check=False
    )
    return result.returncode != 0


def _clone_with_branch(tmp_path: Path, *, subject: str) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run([_GIT, "init", "-q", "--bare", "-b", "main", str(remote)], check=True, env=_clean_env())
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    _git("remote", "add", "origin", str(remote), cwd=work)
    (work / "base.txt").write_text("base\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "initial", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    _git("checkout", "-q", "-b", "feature", cwd=work)
    (work / "feat.txt").write_text("feature\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", subject, cwd=work)
    return work


@pytest.mark.parametrize("config", _CONFIGS, ids=lambda path: path.name)
class TestNoCommitOnMergedBranchHook:
    def test_a_branch_whose_upstream_was_deleted_is_refused(self, tmp_path: Path, config: Path) -> None:
        work = _clone_with_branch(tmp_path, subject="feat: the feature")
        _git("push", "-q", "-u", "origin", "feature", cwd=work)
        _git("push", "-q", "origin", "--delete", "feature", cwd=work)
        _git("fetch", "-q", "--prune", "origin", cwd=work)

        assert _hook_refuses(work, config)

    def test_a_head_subject_naming_gone_does_not_refuse(self, tmp_path: Path, config: Path) -> None:
        work = _clone_with_branch(tmp_path, subject="fix(cleanup): gate the [gone]-branch prune")
        _git("push", "-q", "-u", "origin", "feature", cwd=work)

        assert not _hook_refuses(work, config)

    def test_a_branch_with_no_upstream_is_allowed(self, tmp_path: Path, config: Path) -> None:
        work = _clone_with_branch(tmp_path, subject="feat: never pushed")

        assert not _hook_refuses(work, config)

    def test_a_detached_head_is_allowed(self, tmp_path: Path, config: Path) -> None:
        work = _clone_with_branch(tmp_path, subject="feat: the feature")
        _git("checkout", "-q", "--detach", cwd=work)

        assert not _hook_refuses(work, config)
