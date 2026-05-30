"""Tests for the no-commit sub-agent detector (#1205).

Real ``git`` under ``tmp_path`` is the project's standard pattern. Each test
builds a bare ``origin`` remote and a clone so ``git.default_branch`` (which
reads ``refs/remotes/origin/HEAD``) can resolve the base the detector compares
the work branch against — the same ``origin/<default>`` base the hollow-ship
gate uses. The detector is pure detection over real git plumbing, so the only
thing exercised is its verdict against an actual repository state.
"""

import os
import subprocess
from pathlib import Path

from teatree.hooks.no_commit_detector import NoCommitVerdict, detect

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=_GIT_ENV)  # noqa: S607


def _make_clone_with_origin(tmp_path: Path) -> Path:
    """Build a bare ``origin`` on ``main`` and a clone whose origin/HEAD resolves.

    Returns the clone path, sitting on ``main`` with one initial commit, an
    ``origin`` remote, and a resolvable ``refs/remotes/origin/HEAD``.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=_GIT_ENV)  # noqa: S607
    (seed / "README.md").write_text("init\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True, env=_GIT_ENV)  # noqa: S607

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, env=_GIT_ENV)  # noqa: S607
    _git(clone, "remote", "set-head", "origin", "main")
    return clone


def _add_commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", f"add {name}")


class TestDetectFlagsEmptyWorkBranch:
    def test_work_branch_with_zero_commits_is_flagged(self, tmp_path: Path) -> None:
        clone = _make_clone_with_origin(tmp_path)
        _git(clone, "checkout", "-q", "-b", "1205-feat-thing")

        finding = detect(str(clone))

        assert finding.verdict is NoCommitVerdict.TERMINATED_WITHOUT_COMMIT
        assert finding.is_flagged
        assert finding.branch == "1205-feat-thing"
        assert finding.base == "origin/main"

    def test_uncommitted_edits_on_work_branch_still_flagged(self, tmp_path: Path) -> None:
        """Editing files without committing is exactly the silent-loss case."""
        clone = _make_clone_with_origin(tmp_path)
        _git(clone, "checkout", "-q", "-b", "1205-feat-thing")
        (clone / "edited.py").write_text("work that will be lost\n", encoding="utf-8")

        finding = detect(str(clone))

        assert finding.verdict is NoCommitVerdict.TERMINATED_WITHOUT_COMMIT
        assert finding.is_flagged


class TestDetectDoesNotFlagCommittedWork:
    def test_work_branch_with_one_commit_is_not_flagged(self, tmp_path: Path) -> None:
        clone = _make_clone_with_origin(tmp_path)
        _git(clone, "checkout", "-q", "-b", "1205-feat-thing")
        _add_commit(clone, "feature.py")

        finding = detect(str(clone))

        assert finding.verdict is NoCommitVerdict.COMMITTED
        assert not finding.is_flagged


class TestDetectDoesNotFlagReadonlyReview:
    def test_detached_head_review_worktree_is_not_flagged(self, tmp_path: Path) -> None:
        """A read-only review worktree checks out a SHA in detached HEAD."""
        clone = _make_clone_with_origin(tmp_path)
        _git(clone, "checkout", "-q", "--detach", "HEAD")

        finding = detect(str(clone))

        assert finding.verdict is NoCommitVerdict.NOT_A_WORK_BRANCH
        assert not finding.is_flagged

    def test_sitting_on_base_branch_is_not_flagged(self, tmp_path: Path) -> None:
        clone = _make_clone_with_origin(tmp_path)
        # Stays on `main` — a base checkout, not a work branch.

        finding = detect(str(clone))

        assert finding.verdict is NoCommitVerdict.NOT_A_WORK_BRANCH
        assert not finding.is_flagged


class TestDetectFailsOpen:
    def test_path_that_is_not_a_git_repo_is_undetermined(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()

        finding = detect(str(not_a_repo))

        assert finding.verdict is NoCommitVerdict.UNDETERMINED
        assert not finding.is_flagged

    def test_empty_path_is_undetermined(self) -> None:
        finding = detect("")

        assert finding.verdict is NoCommitVerdict.UNDETERMINED
        assert not finding.is_flagged

    def test_work_branch_without_resolvable_origin_is_undetermined(self, tmp_path: Path) -> None:
        """No ``origin`` remote ⇒ base undetectable ⇒ fail open, never flag."""
        repo = tmp_path / "no-remote"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=_GIT_ENV)  # noqa: S607
        (repo / "README.md").write_text("init\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "init")
        _git(repo, "checkout", "-q", "-b", "1205-feat-thing")

        finding = detect(str(repo))

        assert finding.verdict is NoCommitVerdict.UNDETERMINED
        assert not finding.is_flagged
