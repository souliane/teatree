"""Low-level git subprocess runners shared by every git-concern module.

This is the primitive partition of :mod:`teatree.utils.git`: the three thin
``git -C <repo> ...`` runners (lenient, strict, boolean) plus the ``GIT_*``
env-stripping helper. Every sibling git module (``git_branch``, ``git_commit``,
``git_status``, ``git_sync``, ``git_worktree``, ``git_remote_ops``) imports its
runners from here, so the runners live in exactly one place and a test that
patches the subprocess boundary (``teatree.utils.run``) intercepts all of them.
"""

import contextlib
import os
from collections.abc import Iterator

from teatree.utils.run import run_allowed_to_fail, run_checked


def run(*, repo: str = ".", args: list[str]) -> str:
    result = run_allowed_to_fail(["git", "-C", repo, *args], expected_codes=None)
    return result.stdout.strip()


def run_strict(*, repo: str = ".", args: list[str]) -> str:
    result = run_checked(["git", "-C", repo, *args])
    return result.stdout.strip()


def check(*, repo: str = ".", args: list[str]) -> bool:
    return run_allowed_to_fail(["git", "-C", repo, *args], expected_codes=None).returncode == 0


def git_env_without_overrides() -> dict[str, str]:
    """Process env with every ``GIT_*`` variable stripped.

    A git hook (pre-commit, pre-push) runs under an outer ``git`` that exports
    ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_WORK_TREE``. Inherited by a child
    ``git -C <other-repo>`` call these hijack it onto the outer repo, so a
    command meant for another repo silently operates on the ambient one. Any
    ``git`` call that targets an explicit repo from inside a possible hook
    context must run with this env so it stays hermetic.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


@contextlib.contextmanager
def git_env_hermetic() -> Iterator[None]:
    """Strip every ``GIT_*`` override from ``os.environ`` for the duration, restoring after.

    The mutating sibling of :func:`git_env_without_overrides`, for a caller that
    cannot pass an explicit ``env=`` — a child spawned by a library that merges
    ``os.environ`` under the caller's overrides (e.g. the claude-agent-sdk
    transport builds ``{**os.environ, ..., **options.env}``, a merge that cannot
    DELETE a key the overrides omit). A dispatch fired from inside a git hook
    inherits ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_WORK_TREE``; removing them from
    ``os.environ`` for the spawn window is the only point such a child inherits a
    hermetic environment. They are restored on exit so the rest of the process is
    unaffected.
    """
    stripped = {k: v for k, v in os.environ.items() if k.startswith("GIT_")}
    for key in stripped:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(stripped)
