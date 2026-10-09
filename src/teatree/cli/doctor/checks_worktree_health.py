"""Registered worktrees must be real git checkouts under one root (#3583).

Two silent decays this surfaces. A registered ``Worktree`` row whose dir was
never a checkout at all is dead — every git-driven pass over it (hook
installation, branch classification, teardown) fails with a WARN nobody reads,
and the row keeps the dir alive in every listing. And a worktree living outside
the canonical worktree root splits the namespace the reaper and this doctor
scan, so broken checkouts accumulate in the half nothing sweeps.

A dir this venue merely cannot resolve is neither: it WARNs as unverified and
names no destructive remedy, because the same evidence is produced by a healthy
checkout whose admin dir was recorded in another execution context.

A third decay is the largest and was reported by nothing: a row whose directory is
GONE. Both checks above filter to rows whose dir EXISTS — right for the verdicts
they render, and it left the bulk of the registry invisible, measured at 114 of 137
rows on a live box. The ledger stops being an inventory and nothing says so, which
is what :func:`_check_registered_worktrees_have_a_checkout` reports.

No finding names a remedy that cannot run: the split-namespace WARN asks the
relocate policy which rows ``workspace relocate`` would actually move, and
prescribes it only for those (#4368); the vanished-checkout WARN names only the
read-only disposition report, because absence is never proof of deadness.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.core.models import Worktree
    from teatree.core.worktree.venue import VenueObservation

#: How many rows a finding names before it trails off — enough to act on, short enough to read.
_PREVIEW_ROWS = 5


def _registered_path_observation(path: Path, canonical_root: Path) -> "VenueObservation | None":
    from teatree.core.worktree.venue import (  # noqa: PLC0415 — keeps the pre-Django doctor import lightweight
        VenueObservation,
        observe,
    )

    path = path.expanduser()
    canonical_root = canonical_root.expanduser()
    if path.is_dir():
        return VenueObservation.PRESENT
    if path.exists() or path.is_symlink():
        return None
    if canonical_root.is_dir() and path.is_relative_to(canonical_root):
        return VenueObservation.ABSENT
    return observe(path)


def _check_registered_worktrees_are_checkouts() -> bool:
    """FAIL on a registered worktree dir PROVED not to be a checkout; WARN when it could not be judged.

    A missing dir is NOT a failure here: absence is not proof of anything one
    context can act on, so no destructive remedy follows from it — ``workspace
    release-dead-rows`` reports such a row as unverifiable and keeps it. The
    failure is a dir that EXISTS and never claimed to be a checkout at all.

    The three-valued probe is shared with the reaper on purpose, and so is the
    clone it consults. FAILing on a merely-inconclusive probe would print a
    DESTRUCTIVE remedy for a state no reaper is allowed to act on — a doctor
    prescribing what the factory cannot do, against work that is very likely live.
    """
    from teatree.core.models import Worktree  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.core.worktree.broken_checkout import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        unresolved_checkout_reason,
    )
    from teatree.core.worktree.clone_paths import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        resolve_clone_path,
    )
    from teatree.core.worktree.worktree_roots import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        CheckoutState,
        canonical_worktree_root,
        probe_checkout,
    )

    workspace = canonical_worktree_root()
    present = [
        (worktree, probe_checkout(Path(worktree.worktree_path), clone=resolve_clone_path(workspace, worktree)))
        for worktree in Worktree.objects.all()
        if worktree.worktree_path and Path(worktree.worktree_path).is_dir()
    ]
    broken = [worktree for worktree, state in present if state is CheckoutState.NOT_A_CHECKOUT]
    unverified = [worktree for worktree, state in present if state is CheckoutState.INCONCLUSIVE]
    for worktree in broken:
        typer.echo(
            f"FAIL  Registered worktree {worktree.pk} at {worktree.worktree_path} never was a git checkout "
            "(no .git entry, and git agrees there is no repository) — every git-driven pass over it silently "
            "no-ops. Fix: t3 <overlay> workspace release-dead-rows --apply (releases the ROW only — no dir, "
            "branch, container or database touched), or t3 <overlay> workspace clean-all to also "
            "sweep the dir and every other stale artifact."
        )
    for worktree in unverified:
        typer.echo(
            f"WARN  Registered worktree {worktree.pk} at {worktree.worktree_path} UNVERIFIED: "
            f"{unresolved_checkout_reason(Path(worktree.worktree_path))}. Nothing reaps it — deletion needs "
            "positive proof of deadness and this is not it. Judge it from the venue that owns the path."
        )
    return not broken


def _check_registered_worktrees_have_a_checkout() -> bool:
    """WARN on registered rows whose directory is GONE — reporting only, never a gate.

    Split by what this venue is entitled to conclude, because one lump is how a
    container-only checkout came to read as lost work. ``ABSENT`` is absence this
    process READ: either the immediate neighbourhood or the canonical worktree
    root is readable and the checkout is not in it. The latter keeps a checkout
    absent after cleanup also prunes its empty ticket directory. ``UNOBSERVABLE``
    is a subtree outside that owned root that was never mounted here, which a
    perfectly healthy checkout in another execution context produces identically.

    Neither authorises anything. The only command named is the read-only
    ``release-dead-rows`` disposition report, which KEEPS each row. Absence is
    not proof of deadness — that is the same standard the reapers hold, stated on
    the surface an operator reads.
    """
    from teatree.core.models import Worktree  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.core.worktree.venue import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        VenueObservation,
    )
    from teatree.core.worktree.worktree_roots import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        canonical_worktree_root,
    )

    canonical_root = canonical_worktree_root()
    seen = [
        (
            worktree,
            _registered_path_observation(Path(worktree.worktree_path), canonical_root),
        )
        for worktree in Worktree.objects.all()
        if worktree.worktree_path
    ]
    absent = [worktree for worktree, state in seen if state is VenueObservation.ABSENT]
    unobservable = [worktree for worktree, state in seen if state is VenueObservation.UNOBSERVABLE]
    wrong_kind = [worktree for worktree, state in seen if state is None]
    if absent:
        typer.echo(
            f"WARN  {len(absent)} of {len(seen)} registered worktree row(s) point at a directory this venue "
            f"READ as absent — the ledger is that far from being an inventory: {_row_preview(absent)}. Nothing "
            "reaps them: absence is not proof of deadness, and the branch each names may still hold work. Read "
            "the per-row dispositions with t3 <overlay> workspace release-dead-rows (it keeps every one of "
            "them)."
        )
    if unobservable:
        typer.echo(
            f"WARN  {len(unobservable)} registered worktree row(s) name a subtree this venue never mounted, so "
            f"whether their checkout exists is UNKNOWN here: {_row_preview(unobservable)}. A live checkout in "
            "another execution context reads exactly like this, so it is missing evidence rather than drift — "
            "judge them from the venue that owns those paths."
        )
    if wrong_kind:
        typer.echo(
            f"WARN  {len(wrong_kind)} registered worktree row(s) point at a path that exists here but is not "
            f"a directory: {_row_preview(wrong_kind)}. This is neither a vanished checkout nor an unmounted "
            "subtree; repair the recorded path from the venue that owns it."
        )
    return True


def _row_preview(worktrees: list["Worktree"]) -> str:
    """The first few rows as ``<pk>:<branch>``, so a reader can act without a DB query."""
    shown = ", ".join(f"{worktree.pk}:{worktree.branch or '<no branch>'}" for worktree in worktrees[:_PREVIEW_ROWS])
    return f"{shown}, …" if len(worktrees) > _PREVIEW_ROWS else shown


def _check_one_worktree_root() -> bool:
    """WARN when registered worktrees live outside the canonical worktree root.

    Advisory, not a gate: an operator may deliberately keep a worktree elsewhere
    mid-migration. The point is that the split is NAMED, so the accumulation in
    the unscanned half stops being invisible.

    The remedy is prescribed only for the rows ``workspace relocate`` would
    actually move (#4368). A row it refuses — one across a mount-point boundary,
    a live mid-task checkout, a dirty one — is NAMED with that reason instead of
    counted, because a count that includes it prescribes a command that provably
    cannot discharge the finding, and the WARN then recurs forever at that number.
    """
    from teatree.core.worktree.relocation import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        RelocationCandidate,
        active_cwd,
        relocation_refusal,
    )
    from teatree.core.worktree.worktree_roots import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        canonical_worktree_root,
        worktrees_outside_the_canonical_root,
    )

    outside = worktrees_outside_the_canonical_root()
    if not outside:
        return True
    canonical = canonical_worktree_root()
    active_path = active_cwd()
    refused: list[tuple[str, str]] = []
    for worktree in outside:
        candidate = RelocationCandidate.of(worktree, Path(worktree.worktree_path))
        reason = relocation_refusal(candidate, canonical, active_path=active_path)
        if reason is not None:
            refused.append((str(worktree.worktree_path), reason))
    movable = len(outside) - len(refused)
    if movable:
        typer.echo(
            f"WARN  {movable} of {len(outside)} registered worktree(s) live outside the canonical root "
            f"{canonical} and CAN be relocated — until they are, the reaper and doctor scan a split "
            "namespace. Fix: t3 <overlay> workspace relocate."
        )
    if refused:
        detail = "; ".join(f"{path}: {reason}" for path, reason in refused)
        typer.echo(
            f"WARN  {len(refused)} registered worktree(s) live outside the canonical root {canonical} that "
            f"relocate refuses to move, so it can never discharge them: {detail}."
        )
    return True


def _check_occupied_checkouts() -> bool:
    """Report every checkout a live agent holds (#3952) — informational, never a failure.

    An occupied checkout is the system working as designed, so this is INFO: it
    exists so a second actor wondering why its request was refused can see WHO
    holds the tree without reading the DB, and so a claim outliving its holder is
    visible rather than only discoverable at the next refusal.
    """
    from teatree.core.worktree.occupancy import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        held_worktrees,
    )

    for worktree, holder in held_worktrees():
        typer.echo(
            f"INFO  Checkout {worktree.worktree_path or '<unprovisioned>'} is held by {holder.describe()}. "
            "A second agent is refused it rather than sharing it. Release with "
            f"t3 <overlay> worktree release-occupancy {worktree.worktree_path} once the holder is gone."
        )
    return True


def check_worktree_health() -> bool:
    """Every worktree-health check, each evaluated so none masks the others.

    An unreadable worktree registry (no DB, a migration mid-flight) WARNs rather
    than failing the doctor run: this check reports on state it reads, so being
    unable to read it is "unverified", never "broken".
    """
    try:
        return all(
            (
                _check_registered_worktrees_are_checkouts(),
                _check_registered_worktrees_have_a_checkout(),
                _check_one_worktree_root(),
                _check_occupied_checkouts(),
            )
        )
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Worktree health UNVERIFIED: the worktree registry could not be read ({exc}).")
        return True


__all__ = ["check_worktree_health"]
