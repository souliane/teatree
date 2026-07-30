"""Release registered ``Worktree`` rows whose checkout is provably dead — and nothing else.

``t3 doctor check`` FAILs on a registered row whose directory never was a git checkout:
every git-driven pass over it silently no-ops, so the row is dead weight that also
skews the concurrency cap and ``workspace relocate``. The remedy it prints is
``workspace clean-all``, which does resolve it — as one pass inside a sweep that also
prunes local branches, drops databases, reaps docker projects, and removes
directories. An operator who needs only the rows cleared should not have to authorise
all of that, and on a host running a live stack the wider blast radius is the reason
they will not run it at all.

This module is the narrow alternative, and "narrow" describes the blast radius, never
the safety bar. It reuses BOTH of the sweep's decisions unchanged: the protection
gates it must clear first (:func:`~teatree.core.cleanup.reap_pre_gates.reap_pre_gate`
— ``clean_ignore``, ownership, liveness) and then the classifier that judges the dead
checkout (:func:`~teatree.core.worktree.broken_checkout.classify_broken_checkout`, so
the #706 data-loss standard and its freshness precondition are identical). Only then
does it delete the DB row alone: no directory removed, no branch deleted, no
container, image, database or stash touched. Anything either step cannot positively
clear is KEPT with its reason reported.

The gates are not made moot by the checkout dying. A ``reaper_pinned`` pin, a
``clean_ignore`` entry and a busy ticket are all decided off the database or the
source clone, so a row mid-delivery is exactly as live as it was before its files
went — and releasing it would drop the operator's protection precisely where the
sweep honours it.

Dry-run is the caller's explicit choice and the CLI's default; the preview and the
live run resolve their row set through :func:`plan_dead_row_release`, so they cannot
disagree about scope.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from teatree.core.cleanup.cleanup import _resolve_worktree_path
from teatree.core.cleanup.reap_pre_gates import reap_pre_gate
from teatree.core.models import Worktree
from teatree.core.worktree.broken_checkout import (
    BrokenCheckout,
    BrokenCheckoutVerdict,
    RemoteRefresh,
    classify_broken_checkout,
)


class DeadRowDisposition(StrEnum):
    """Why a dead-checkout row may or may not be released."""

    RELEASABLE = "releasable"
    HOLDS_WORK = "holds-work"
    UNVERIFIABLE = "unverifiable"
    PROTECTED = "protected"


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
    output is the set of rows the doctor's finding is about. The classifier decides
    that SCOPE question; :func:`_verdict_for` then decides the row's disposition.
    """
    refresh = RemoteRefresh()
    verdicts: list[DeadRowVerdict] = []
    for row in Worktree.objects.select_related("ticket").order_by("pk"):
        checkout = classify_broken_checkout(row, workspace=workspace, refresh=refresh)
        if checkout.state is BrokenCheckout.LIVE_CHECKOUT:
            continue
        verdicts.append(_verdict_for(row, workspace=workspace, checkout=checkout))
    return verdicts


def _verdict_for(row: Worktree, *, workspace: Path, checkout: BrokenCheckoutVerdict) -> DeadRowVerdict:
    """One in-scope row's disposition — a protection gate first, else the classifier.

    The gate takes precedence for the same reason ``clean-all`` consults it first:
    it is why the row is kept, so it is what the operator has to read. Reporting a
    pinned row as merely "holds work" would name the wrong obstacle, and reporting
    it as releasable would be the defect this precedence exists to close.
    """
    gate = reap_pre_gate(row, workspace=workspace)
    return DeadRowVerdict(
        worktree_pk=int(row.pk),
        branch=row.branch,
        path=str(_resolve_worktree_path(workspace, row)),
        disposition=DeadRowDisposition.PROTECTED if gate else _FROM_CHECKOUT_STATE[checkout.state],
        reason=gate.reason if gate else checkout.reason,
    )


@dataclass(frozen=True, slots=True)
class DeadRowReleaseOutcome:
    """What the pass classified and what it actually deleted.

    The engine returns DATA and renders on request, the way ``run_relocate`` does,
    so the CLI can emit a structured payload on the machine channel and the same
    verdicts as prose on the human one — one classification, two renderings, which
    is what stops the two surfaces disagreeing about scope.
    """

    applied: bool
    verdicts: tuple[DeadRowVerdict, ...]
    released_pks: frozenset[int]

    def render(self) -> list[str]:
        """One operator-readable line per classified row."""
        lines: list[str] = []
        for verdict in self.verdicts:
            if not verdict.releasable:
                lines.append(f"KEPT '{verdict.branch}' (worktree {verdict.worktree_pk}): {verdict.reason}")
            elif verdict.worktree_pk in self.released_pks:
                lines.append(
                    f"Released row for '{verdict.branch}' (worktree {verdict.worktree_pk}); "
                    f"directory and branch left untouched at {verdict.path}"
                )
            else:
                lines.append(f"WOULD RELEASE '{verdict.branch}' (worktree {verdict.worktree_pk}): {verdict.reason}")
        return lines


def release_dead_rows(workspace: Path, *, dry_run: bool) -> DeadRowReleaseOutcome:
    """Release the provably-dead rows, or preview them under *dry_run*.

    The delete is a plain row delete — never ``cleanup_worktree``, whose job is to
    tear down the git worktree, the branch, the database and the docker artifacts.
    Here the checkout is already gone and the surviving on-disk directory may hold
    files nothing has proven redundant, so removing it is a separate decision that
    belongs to the operator (or to the broken-DIR pass), not to a row release.
    """
    verdicts = tuple(plan_dead_row_release(workspace))
    released: set[int] = set()
    for verdict in verdicts:
        if not verdict.releasable or dry_run:
            continue
        Worktree.objects.filter(pk=verdict.worktree_pk).delete()
        released.add(verdict.worktree_pk)
    return DeadRowReleaseOutcome(applied=not dry_run, verdicts=verdicts, released_pks=frozenset(released))


__all__ = [
    "DeadRowDisposition",
    "DeadRowReleaseOutcome",
    "DeadRowVerdict",
    "plan_dead_row_release",
    "release_dead_rows",
]
