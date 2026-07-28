"""Release registered ``Worktree`` rows whose checkout is provably dead — and nothing else.

``t3 doctor check`` FAILs on a registered row whose directory is not a git checkout:
every git-driven pass over it silently no-ops, so the row is dead weight that also
skews the concurrency cap and ``workspace relocate``. The remedy it prints is
``workspace clean-all``, which does resolve it — as one pass inside a sweep that also
prunes local branches, drops databases, reaps docker projects, and removes
directories. An operator who needs only the rows cleared should not have to authorise
all of that, and on a host running a live stack the wider blast radius is the reason
they will not run it at all.

This module is the narrow alternative: it reuses the SAME classifier the sweep uses
(:func:`~teatree.core.worktree.broken_checkout.classify_broken_checkout`, so the #706
data-loss standard and its freshness precondition are identical), and then deletes
the DB row alone. It removes no directory, deletes no branch, touches no container,
image, database or stash. Anything the classifier cannot positively clear is KEPT
with its reason reported.

Dry-run is the caller's explicit choice and the CLI's default; the preview and the
live run resolve their row set through :func:`plan_dead_row_release`, so they cannot
disagree about scope.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from teatree.core.cleanup.cleanup import _resolve_worktree_path
from teatree.core.models import Worktree
from teatree.core.worktree.broken_checkout import BrokenCheckout, RemoteRefresh, classify_broken_checkout


class DeadRowDisposition(StrEnum):
    """Why a dead-checkout row may or may not be released."""

    RELEASABLE = "releasable"
    HOLDS_WORK = "holds-work"
    UNVERIFIABLE = "unverifiable"


_FROM_CHECKOUT_STATE = {
    BrokenCheckout.RELEASABLE: DeadRowDisposition.RELEASABLE,
    BrokenCheckout.HOLDS_WORK: DeadRowDisposition.HOLDS_WORK,
    BrokenCheckout.UNVERIFIABLE: DeadRowDisposition.UNVERIFIABLE,
}


@dataclass(frozen=True, slots=True)
class DeadRowVerdict:
    """One registered row's disposition, with the identifiers an operator reads."""

    worktree_pk: int
    branch: str
    path: str
    disposition: DeadRowDisposition
    reason: str

    @property
    def releasable(self) -> bool:
        return self.disposition is DeadRowDisposition.RELEASABLE


def plan_dead_row_release(workspace: Path) -> list[DeadRowVerdict]:
    """Classify every registered row whose checkout is dead. Read-only.

    A row with a LIVE checkout — or no directory at all — is not this pass's
    business and is omitted entirely rather than reported as a non-candidate: the
    output is the set of rows the doctor's finding is about.
    """
    refresh = RemoteRefresh()
    verdicts: list[DeadRowVerdict] = []
    for row in Worktree.objects.select_related("ticket").order_by("pk"):
        verdict = classify_broken_checkout(row, workspace=workspace, refresh=refresh)
        disposition = _FROM_CHECKOUT_STATE.get(verdict.state)
        if disposition is None:
            continue
        verdicts.append(
            DeadRowVerdict(
                worktree_pk=int(row.pk),
                branch=row.branch,
                path=str(_resolve_worktree_path(workspace, row)),
                disposition=disposition,
                reason=verdict.reason,
            )
        )
    return verdicts


def release_dead_rows(workspace: Path, *, dry_run: bool) -> list[str]:
    """Release the provably-dead rows (or preview them), returning one line each.

    The delete is a plain row delete — never ``cleanup_worktree``, whose job is to
    tear down the git worktree, the branch, the database and the docker artifacts.
    Here the checkout is already gone and the surviving on-disk directory may hold
    files nothing has proven redundant, so removing it is a separate decision that
    belongs to the operator (or to the broken-DIR pass), not to a row release.
    """
    lines: list[str] = []
    for verdict in plan_dead_row_release(workspace):
        if not verdict.releasable:
            lines.append(f"KEPT '{verdict.branch}' (worktree {verdict.worktree_pk}): {verdict.reason}")
            continue
        if dry_run:
            lines.append(f"WOULD RELEASE '{verdict.branch}' (worktree {verdict.worktree_pk}): {verdict.reason}")
            continue
        Worktree.objects.filter(pk=verdict.worktree_pk).delete()
        lines.append(
            f"Released row for '{verdict.branch}' (worktree {verdict.worktree_pk}); "
            f"directory and branch left untouched at {verdict.path}"
        )
    return lines


__all__ = ["DeadRowDisposition", "DeadRowVerdict", "plan_dead_row_release", "release_dead_rows"]
