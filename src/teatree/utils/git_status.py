"""Working-tree status reads and the full-worktree diff capture.

The status/diff partition of :mod:`teatree.utils.git`. Holds the porcelain
status reads (lenient and fail-closed) and the data-loss-guard diff capture
(#835), all via the :mod:`teatree.utils.git_run` runners.
"""

from teatree.utils.git_run import git_env_without_overrides, run, run_strict
from teatree.utils.run import run_checked


def status_porcelain(repo: str = ".") -> str:
    return run(repo=repo, args=["status", "--porcelain"])


def status_porcelain_strict(repo: str = ".") -> str:
    """Like :func:`status_porcelain` but raises on a non-zero ``git status`` exit.

    :func:`status_porcelain` swallows git errors and returns whatever (possibly
    empty) stdout it got, so an inconclusive status (lock contention, corrupt
    index, missing dir) is indistinguishable from a genuinely clean tree. For a
    data-loss decision that must fail closed, use this variant: a git error
    raises ``CommandFailedError`` so the caller can treat "couldn't determine"
    as "might be dirty" rather than "clean".
    """
    return run_strict(repo=repo, args=["status", "--porcelain"])


def status_porcelain_z_strict(repo: str = ".") -> str:
    """``git status --porcelain -z`` verbatim — NUL-terminated fields, never stripped.

    The text form C-quotes any path holding a space or a quote and renders a
    rename as ``old -> new``, so splitting it yields strings that name no file.
    ``-z`` emits every path raw in its own NUL-terminated field and keeps a
    rename's two endpoints as two adjacent fields. Its records are
    column-fixed (``XY <path>``), which is why — unlike
    :func:`status_porcelain_strict` — the output must NOT be stripped: a leading
    strip would eat the staged column of an unstaged-only entry (``" M path"``)
    and shift every path by one character.

    Fail-closed like :func:`status_porcelain_strict`: a non-zero ``git status``
    raises rather than reading as a clean tree.
    """
    return run_checked(["git", "-C", repo, "status", "--porcelain", "-z"]).stdout


def full_worktree_diff(repo: str, base: str = "HEAD") -> str:
    """Return a single patch covering staged, unstaged, AND untracked changes.

    ``git diff HEAD`` alone omits untracked files. Marking them intent-to-add
    (``git add -N``) makes them appear in the diff as new-file hunks (without
    staging their content), so a single ``git apply`` of the returned patch
    restores edits and brand-new files alike. The intent-to-add marks are
    harmless: the worktree is about to be removed.

    ``base`` is the revision the working tree is diffed against — ``HEAD`` for
    a normal worktree. A dangling-HEAD worktree (forge post-merge ref deletion)
    has no resolvable ``HEAD`` (``git diff HEAD`` exits 128), so the caller
    passes the recovered tip SHA instead, so the patch still captures the
    genuine uncommitted delta on top of that tip.

    The prefixes are forced explicitly with ``--src-prefix=a/
    --dst-prefix=b/``: ``git diff`` otherwise honours the caller's git config,
    and a user with ``diff.noprefix=true`` (common) would get a patch with no
    ``a/``/``b/`` prefixes that a plain ``git apply`` cannot restore — total
    loss of the captured work, the exact #835 scenario. Forcing the prefixes
    keeps the patch standard and ``git apply``-able regardless of user config.
    """
    env = git_env_without_overrides()
    run_checked(["git", "-C", repo, "add", "-A", "-N"], env=env)
    result = run_checked(
        ["git", "-C", repo, "diff", base, "--binary", "--src-prefix=a/", "--dst-prefix=b/"],
        env=env,
    )
    return result.stdout
