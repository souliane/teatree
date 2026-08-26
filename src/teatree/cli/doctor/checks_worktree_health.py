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

A third decay is one no ``Worktree`` row can see: a REGISTRATION git still holds
for a directory that is gone. Those are what make ``git worktree list`` count
phantoms, so anyone reasoning about capacity or "what is still checked out" reads
an inflated number (#4576).

Neither finding names a remedy that cannot run: the split-namespace WARN asks the
relocate policy which rows ``workspace relocate`` would actually move, and
prescribes it only for those (#4368).
"""

from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.utils.git import WorktreeRecord

# Enough phantoms to recognise the class without turning one WARN into a wall.
_NAMED_IN_WARN = 5


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
            "positive proof of deadness and this is not it. Recover work with t3 <overlay> workspace salvage."
        )
    return not broken


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


def _stranded_reason(record: "WorktreeRecord", canonical: Path) -> str | None:
    """Why ``clean-all`` cannot clear this phantom, or ``None`` when it can.

    Two mechanisms discharge one: the per-entry review sweep, which needs nothing
    from the venue beyond the directory's absence, and the clone-wide prune, which
    needs the registration to be unlocked and to lie under the root this venue
    provisions into. A phantom neither can reach is NAMED with its reason rather
    than counted, so the WARN cannot prescribe a command that provably will not
    discharge it — the recurring-forever failure #4368 fixed for the sibling check.
    """
    from teatree.core.worktree.review_worktree_reaper import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        is_review_checkout,
    )
    from teatree.core.worktree.venue_safe_registry import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        venue_may_call_absent_dead,
    )

    if is_review_checkout(record):
        if record.lock_reason:
            return f"a review checkout locked with a reason ({record.lock_reason!r}), which the sweep honours"
        return None
    if record.locked:
        return "locked, and `git worktree prune` skips a lock by design — `git worktree unlock` it first"
    if not venue_may_call_absent_dead(record.path):
        return f"outside {canonical}, so the venue-safe prune withholds it — prune from the venue that owns it"
    return None


def _check_phantom_registrations() -> bool:
    """WARN when a clone's registry counts checkouts whose directories are gone.

    Advisory: a registration is dropped only on this venue's own evidence, and a
    directory it merely cannot reach is deliberately not counted here — that is the
    ``UNOBSERVABLE`` case the reapers keep.
    """
    from teatree.core.cleanup.checkout_registry import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        candidate_clones,
    )
    from teatree.core.worktree.venue import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        VenueObservation,
        observe,
    )
    from teatree.core.worktree.worktree_roots import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        canonical_worktree_root,
    )
    from teatree.utils import git  # noqa: PLC0415 — deferred: matches the sibling checks import shape

    canonical = canonical_worktree_root()
    for repo in sorted(candidate_clones(canonical)):
        clearable: list[Path] = []
        stranded: list[tuple[Path, str]] = []
        for record in git.list_worktrees(repo):
            if observe(record.path) is not VenueObservation.ABSENT:
                continue
            reason = _stranded_reason(record, canonical)
            if reason is None:
                clearable.append(record.path)
            else:
                stranded.append((record.path, reason))
        if clearable:
            named = ", ".join(str(path) for path in clearable[:_NAMED_IN_WARN])
            rest = f" and {len(clearable) - _NAMED_IN_WARN} more" if len(clearable) > _NAMED_IN_WARN else ""
            typer.echo(
                f"WARN  {len(clearable)} registration(s) in {repo} name directories that are gone, so "
                f"`git worktree list` counts phantoms: {named}{rest}. Fix: t3 <overlay> workspace clean-all."
            )
        if stranded:
            detail = "; ".join(f"{path}: {reason}" for path, reason in stranded)
            typer.echo(
                f"WARN  {len(stranded)} phantom registration(s) in {repo} that clean-all cannot clear, so it "
                f"can never discharge them: {detail}."
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
                _check_one_worktree_root(),
                _check_phantom_registrations(),
                _check_occupied_checkouts(),
            )
        )
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Worktree health UNVERIFIED: the worktree registry could not be read ({exc}).")
        return True


__all__ = ["check_worktree_health"]
