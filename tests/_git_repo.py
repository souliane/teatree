"""Shared real-git fixture builder, default-branch-independent (souliane/teatree#2359).

Real-git fixtures used to assume ``main`` was the default branch: a bare
``git init`` followed by a ``worktree add <path> main`` / ``checkout main``
passes on a dev box whose config bakes in ``init.defaultBranch=main`` but
exits 128 (``invalid reference: main``) on a CI image whose git defaults to
``master``. The fix is to never rely on the ambient default: born the branch
explicitly with ``git init -b <default_branch>`` and an initial commit, so the
branch is a real ref regardless of the host's ``init.defaultBranch``.

Use :func:`make_git_repo` for any new real-git fixture instead of hand-rolling
``git init``; it makes the default branch an argument, so a test can pin
``main`` (the common case) or any other name without a config dependency.
"""

import os
import shutil
import subprocess
from pathlib import Path

_GIT = shutil.which("git") or "/usr/bin/git"


def git_identity_env() -> dict[str, str]:
    """Process env plus a deterministic commit identity and nulled config sources.

    ``commit`` needs an author/committer identity; a CI sandbox has none.
    Nulling ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` makes the build
    reproduce the CI default-branch condition on a dev box too — so a fixture
    that secretly depends on ``init.defaultBranch=main`` fails here, not only
    on CI.
    """
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    """Run ``git -C <repo> <args>`` with the deterministic identity; return stdout.

    With ``check=False`` a non-zero exit returns the (possibly empty) stdout
    instead of raising — for a probe like ``rev-parse --verify --quiet`` whose
    failure is the expected answer.
    """
    out = subprocess.run(
        [_GIT, "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=git_identity_env(),
    )
    return out.stdout.strip()


def make_git_repo(
    path: Path,
    *,
    default_branch: str = "main",
    initial_commit: bool = True,
    bare: bool = False,
) -> Path:
    """Create a git repo at *path* whose default branch is *default_branch*.

    The branch is born with ``git init -b <default_branch>`` so it exists as a
    real ref no matter what the host's ``init.defaultBranch`` is. With
    ``initial_commit`` (the default) an empty commit is added so the branch has
    a HEAD a ``worktree add`` / ``checkout`` / ``rev-parse`` can resolve.

    A bare repo never has a working tree, so ``initial_commit`` is ignored for
    ``bare=True``.
    """
    path.mkdir(parents=True, exist_ok=True)
    init_args = ["init", "-q", "-b", default_branch]
    if bare:
        init_args.insert(1, "--bare")
    run_git(path, *init_args)
    if initial_commit and not bare:
        run_git(path, "commit", "-q", "--allow-empty", "-m", "initial")
    return path
