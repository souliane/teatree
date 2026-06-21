"""Finalize-time guards for the ``workspace finalize`` command.

The finalize partition of :mod:`teatree.core.management.commands.workspace`.
Holds the defense-in-depth refusal that keeps a finalize soft-reset + commit
off a managed main clone's default branch (#752).
"""

from pathlib import Path

from teatree.paths import running_from_worktree
from teatree.utils import git


def refuse_finalize_on_main_clone_default(repo_dir: str, default_br: str) -> None:
    """Refuse to finalize-commit on a managed main clone's default branch (#752).

    Defense-in-depth guard: when a ``Worktree`` row's path resolves to a managed
    main clone (``.git`` is a *directory*, not a linked worktree's pointer file)
    AND it is checked out on the default branch, a finalize soft-reset + commit
    would land on the shared main clone's default branch. After the PR
    squash-merges on the remote, the rewritten SHA leaves the local default
    branch unable to fast-forward — the main-clone divergence that bricks
    ``t3 update``. Refuse before committing rather than producing it.

    A linked worktree (``.git`` is a pointer file) is never refused, and a main
    clone on a *feature* branch is left alone — only the main-clone + default-
    branch combination is the divergence hazard. When the branch cannot be
    resolved (a non-repo path yields an empty ``current_branch``) the guard
    does not match the default branch, so it does not refuse.
    """
    if running_from_worktree(Path(repo_dir)):
        return
    if git.current_branch(repo_dir) != default_br:
        return
    msg = (
        f"Refusing to finalize-commit on a managed main clone's default branch "
        f"({default_br}) at {repo_dir} — use a worktree; see worktree-first.\n"
        "Create a worktree first: t3 <overlay> workspace ticket <issue_url>"
    )
    raise SystemExit(msg)
