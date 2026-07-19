"""``unsynced_commits_strict`` RAISES on a git failure instead of degrading to [].

The lenient :func:`unsynced_commits` swallows a git failure to ``""`` → an empty
list, which a destructive caller reads as "provably synced — safe to wipe". The
strict twin (#F4.3) surfaces the failure as ``CommandFailedError`` so the caller
can fail closed on an inconclusive probe, while still returning ``[]`` for a
genuinely fully-synced branch.
"""

from pathlib import Path

import pytest

from teatree.utils.git import unsynced_commits, unsynced_commits_strict
from teatree.utils.git_run import run_strict
from teatree.utils.run import CommandFailedError


def _git(repo: Path, *args: str) -> None:
    run_strict(repo=str(repo), args=list(args))


def _repo_with_feature(tmp_path: Path, *, commits_ahead: int) -> Path:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    for i in range(commits_ahead):
        (repo / f"f{i}.txt").write_text(f"{i}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"feat {i}")
    return repo


def test_lists_commits_ahead_of_base(tmp_path: Path) -> None:
    repo = _repo_with_feature(tmp_path, commits_ahead=2)
    result = unsynced_commits_strict(str(repo), "feature", "main")
    assert len(result) == 2


def test_empty_when_fully_synced(tmp_path: Path) -> None:
    repo = _repo_with_feature(tmp_path, commits_ahead=0)
    assert unsynced_commits_strict(str(repo), "feature", "main") == []


def test_raises_when_target_is_unresolvable(tmp_path: Path) -> None:
    # An unresolvable target makes the underlying ``git log`` fail. The strict
    # form RAISES; the lenient form silently returns [] (the data-loss trap).
    repo = _repo_with_feature(tmp_path, commits_ahead=1)
    with pytest.raises(CommandFailedError):
        unsynced_commits_strict(str(repo), "feature", "origin/does-not-exist")
    assert unsynced_commits(str(repo), "feature", "origin/does-not-exist") == []
