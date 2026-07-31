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
"""

from pathlib import Path

import typer


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
    """
    from teatree.core.worktree.worktree_roots import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        canonical_worktree_root,
        worktrees_outside_the_canonical_root,
    )

    outside = worktrees_outside_the_canonical_root()
    if not outside:
        return True
    typer.echo(
        f"WARN  {len(outside)} registered worktree(s) live outside the canonical root "
        f"{canonical_worktree_root()} — the reaper and doctor then scan a split namespace. "
        "Fix: t3 <overlay> workspace relocate."
    )
    return True


def check_worktree_health() -> bool:
    """Both worktree-health checks, each evaluated so neither masks the other.

    An unreadable worktree registry (no DB, a migration mid-flight) WARNs rather
    than failing the doctor run: this check reports on state it reads, so being
    unable to read it is "unverified", never "broken".
    """
    try:
        return all((_check_registered_worktrees_are_checkouts(), _check_one_worktree_root()))
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Worktree health UNVERIFIED: the worktree registry could not be read ({exc}).")
        return True


__all__ = ["check_worktree_health"]
