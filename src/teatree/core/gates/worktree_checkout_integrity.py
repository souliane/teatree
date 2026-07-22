"""Registered worktrees whose recorded dir exists but is not a functional checkout.

A ``Worktree`` row's ``extra['worktree_path']`` should point at a live git
checkout. The reconciler's ``missing_worktree_dirs`` finding only fires when the
dir is GONE; a dir that is still present but whose ``.git`` linkage is severed
(``git rev-parse`` fails) slips past it silently. Such a broken checkout holds no
git-recoverable work and splits the reaper/doctor namespace, so ``t3 doctor``
FAILs on it (#3583). Pure discovery — the CLI layer prints the verdict.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.utils.git_worktree_query import is_broken_checkout


@dataclass(frozen=True)
class BrokenRegisteredCheckout:
    """A ``Worktree`` row whose recorded dir exists but is a broken checkout."""

    worktree_pk: int
    path: Path


def find_broken_registered_checkouts() -> list[BrokenRegisteredCheckout]:
    """Every ``Worktree`` row whose recorded dir is present but a broken checkout.

    A row with no recorded ``worktree_path`` (nothing to probe) or whose dir is
    gone (the reconciler's missing-dir finding owns that) is skipped: this gate
    fires only on the present-but-broken case nothing else surfaces.
    """
    from teatree.core.models import Worktree  # noqa: PLC0415 — deferred: ORM import at call time

    findings: list[BrokenRegisteredCheckout] = []
    for worktree in Worktree.objects.all():
        extra = worktree.extra if isinstance(worktree.extra, dict) else {}
        path_str = str(extra.get("worktree_path", ""))
        if not path_str:
            continue
        path = Path(path_str)
        if path.is_dir() and is_broken_checkout(path):
            findings.append(BrokenRegisteredCheckout(worktree_pk=worktree.pk, path=path))
    return findings


__all__ = ["BrokenRegisteredCheckout", "find_broken_registered_checkouts"]
