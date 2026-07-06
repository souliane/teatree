"""Orphaned-stash reaping for the ``t3 <overlay> workspace clean-all`` subcommand.

Split out of :mod:`teatree.core.management.commands._workspace_cleanup` so that
module stays under the module-health LOC cap. The forge-CLI-free squash-merge
signal it relies on (:func:`_branch_captured_upstream`) lives in
:mod:`teatree.core.worktree.branch_classification` — the branch/worktree reapers share it.
"""

import re

from teatree.core.worktree.branch_classification import _branch_captured_upstream
from teatree.utils import git
from teatree.utils.run import CommandFailedError

_STASH_BRANCH_RE = re.compile(r"^stash@\{\d+\}:\s+(?:WIP on|On)\s+(?P<branch>[^:]+):")


def _stash_branch(line: str) -> str:
    """Return the branch a ``git stash list`` line belongs to, or ``""`` if unparsable.

    A stash taken on a detached HEAD reads ``On (no branch): ...`` — there is no
    owning branch to compare against, so it is reported as unparsable and the
    stash is kept rather than reaped.
    """
    match = _STASH_BRANCH_RE.match(line)
    if not match:
        return ""
    branch = match.group("branch").strip()
    return "" if branch == "(no branch)" else branch


def drop_orphaned_stashes(repo: str) -> list[str]:
    """Drop stashes whose branch is gone — but ONLY when their changes are merged.

    A stash is the *only* copy of its work. Dropping it because its owning branch
    no longer exists is silent data loss when the stashed changes were never
    merged — the exact failure that strands work like the #1913 FSM and dreaming
    phase stashes. So an orphaned stash is reaped only when its diff is already
    captured upstream (the same patch-id squash-merge signal
    :func:`_branch_captured_upstream` gives the worktree/branch reapers); an
    orphaned stash carrying UNMERGED work is KEPT with a warning, never dropped.
    A probe failure reads as not-merged, so uncertainty keeps the stash.
    """
    stash_list = git.run(repo=repo, args=["stash", "list"])
    if not stash_list:
        return []

    existing = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    }
    try:
        default = git.run(repo=repo, args=["rev-parse", "--abbrev-ref", "origin/HEAD"]).strip().removeprefix("origin/")
    except CommandFailedError:
        default = ""
    default = default or "main"

    cleaned: list[str] = []
    entries = stash_list.splitlines()
    # Reverse order so dropping stash@{i} never shifts a not-yet-processed index.
    for i in range(len(entries) - 1, -1, -1):
        line = entries[i]
        label = line.split(":")[0]
        branch = _stash_branch(line)
        if not branch or branch in existing:
            continue
        ref = f"stash@{{{i}}}"
        if not _branch_captured_upstream(repo, ref, default):
            cleaned.append(
                f"Kept orphaned stash {label} (was on {branch}): changes are NOT merged — "
                f"dropping would lose them. Recover with `git stash apply {ref}`."
            )
            continue
        git.run(repo=repo, args=["stash", "drop", ref])
        cleaned.append(f"Dropped orphaned stash: {label} (was on {branch}; changes already merged)")

    return cleaned
