"""Throwaway-git-repo fixtures for the deterministic regression corpus.

The corpus's git-backed checks build real repositories under a
``tempfile.TemporaryDirectory`` so each runs its true ``git`` path — fetch,
merge-tree, rev-parse — against a genuine repo rather than a mock. These
seeding helpers and the ambient-``GIT_*`` neutraliser live here, separate from
the checks themselves, so :mod:`teatree.eval.regression_corpus` stays focused on
the invariants it pins.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from teatree.utils.git import git_env_without_overrides
from teatree.utils.run import run_checked


@contextmanager
def without_git_overrides() -> Iterator[None]:
    """Run the block with every ambient ``GIT_*`` override stripped.

    Mirrors :func:`teatree.utils.git.git_env_without_overrides` for an in-process
    call: the ship path runs outside a git hook, but the corpus's hijacked-env
    test sets ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE``. A production
    ``git -C <repo>`` reader (``current_branch``) would otherwise resolve the
    outer repo, so checks that drive such a reader run hermetically here.
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith("GIT_")}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


def git(repo: Path, *args: str) -> str:
    env = {
        **git_env_without_overrides(),
        "GIT_AUTHOR_NAME": "eval",
        "GIT_AUTHOR_EMAIL": "eval@example.com",
        "GIT_COMMITTER_NAME": "eval",
        "GIT_COMMITTER_EMAIL": "eval@example.com",
    }
    return run_checked(["git", *args], cwd=repo, env=env).stdout


def seed_repo_with_diverging_target(work: Path) -> tuple[Path, str]:
    """Build a repo whose feature SHA conflicts with ``origin/main``.

    Returns ``(repo_path, feature_sha)``. ``origin`` is a sibling clone the
    branch-currency fetch resolves, so the real ``sha_conflicts_with_target``
    runs its true ``git fetch`` + ``git merge-tree`` path against a genuine
    divergence — not a mock.
    """
    origin = work / "origin"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")

    repo = work / "clone"
    git(work, "clone", str(origin), "clone")
    conflicted = repo / "conflict.txt"
    conflicted.write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    git(repo, "push", "origin", "HEAD:main")

    git(repo, "checkout", "-b", "feature")
    conflicted.write_text("feature side\n", encoding="utf-8")
    git(repo, "commit", "-am", "feature edit")
    feature_sha = git(repo, "rev-parse", "HEAD").strip()

    git(repo, "checkout", "main")
    conflicted.write_text("main side\n", encoding="utf-8")
    git(repo, "commit", "-am", "main edit")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "feature")
    return repo, feature_sha


def seed_repo_on_branch(work: Path, branch: str) -> Path:
    """Init a one-commit git repo checked out on *branch* and return its path."""
    repo = work / "wt"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", branch)
    return repo


def unused_pid() -> int:
    """A pid that is (almost certainly) not alive — picks a high free slot."""
    from teatree.utils.singleton import pid_alive  # noqa: PLC0415 — deferred: loaded per eval run

    for candidate in range(2_000_000, 2_000_500):
        if not pid_alive(candidate):
            return candidate
    return 2_147_483_000


class StubBackend:
    """A messaging backend whose ``auth_test`` reports a fixed reachability."""

    def __init__(self, *, ok: bool) -> None:
        self._ok = ok
        self.name = "slack"

    def auth_test(self) -> dict:
        return {"ok": self._ok} if self._ok else {"ok": False, "error": "invalid_auth"}


def seed_repo_behind_but_clean(work: Path) -> tuple[Path, str]:
    """Build a repo whose feature SHA is behind ``origin/main`` but conflict-free."""
    origin = work / "origin2"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")

    repo = work / "clone2"
    git(work, "clone", str(origin), "clone2")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    git(repo, "push", "origin", "HEAD:main")

    git(repo, "checkout", "-b", "feature")
    feature_sha = git(repo, "rev-parse", "HEAD").strip()

    git(repo, "checkout", "main")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")  # disjoint file — no conflict
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "main advances disjointly")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "feature")
    return repo, feature_sha
