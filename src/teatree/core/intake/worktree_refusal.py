"""What the resolver says when no worktree can be resolved.

A refusal that names no reachable verb dead-ends, and the old one did worse than
that: it told an operator standing INSIDE a worktree to go stand inside a worktree.
The cause decides the remedy, so the classification comes first and the advice
follows from it.
"""

from teatree.core.worktree.checkout_gitdir import CheckoutKind, classify_checkout, gitdir_pointer

_ADOPT = "Register an existing checkout with `t3 <overlay> worktree adopt <path>` (it provisions nothing)."


def unresolvable_worktree_message(cwd: str) -> str:
    """The ``WorktreeNotFoundError`` text for *cwd*, naming the cause and the remedy."""
    head = f"Cannot auto-detect worktree from {cwd}."
    kind = classify_checkout(cwd)
    if kind is CheckoutKind.UNREACHABLE_GITDIR:
        return (
            f"{head}\nIt is a linked git worktree, but its gitdir {gitdir_pointer(cwd)} does not exist here, "
            "so no git command can answer for it. The clone it was cut from is not visible in this venue — "
            "mount it at the same path, or re-cut the worktree from a clone that is."
        )
    if kind is CheckoutKind.MAIN_CLONE:
        return (
            f"{head}\nThis is a main clone, not a worktree. Cut one with `t3 <overlay> workspace ticket <issue_url>`."
        )
    return f"{head}\nMake sure you are running t3 from inside a worktree directory.\n{_ADOPT}"
