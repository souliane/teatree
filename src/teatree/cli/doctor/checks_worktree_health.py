"""Registered worktrees must be real git checkouts under one durable root (#3583, #4194).

Three silent decays this surfaces. A registered ``Worktree`` row whose dir was
never a checkout at all is dead — every git-driven pass over it (hook
installation, branch classification, teardown) fails with a WARN nobody reads,
and the row keeps the dir alive in every listing. And a worktree living outside
the canonical worktree root splits the namespace the reaper and this doctor
scan, so broken checkouts accumulate in the half nothing sweeps. And a worktree
on a session- or job-scoped path is pruned with its session, so unpushed work
there survives nowhere — five such checkouts went unnamed by any check (#4194).

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


def _check_no_session_scoped_worktrees() -> bool:
    """WARN naming each registered worktree whose checkout dies with a session (#4194).

    Advisory, like the split-root check beside it: the point is that a worktree on
    a session- or job-scoped path is NAMED. Five holding unpushed work were found
    under a dead session's job dir, and nothing surfaced them — the registry knew
    the path, no check read it.
    """
    from teatree.core.models import Worktree  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.utils.volatile_checkout import (  # noqa: PLC0415 — deferred: keeps the cold-import surface stdlib
        durable_checkout_root,
        volatile_reason,
    )

    for worktree in Worktree.objects.all():
        reason = volatile_reason(Path(worktree.worktree_path)) if worktree.worktree_path else ""
        if not reason:
            continue
        typer.echo(
            f"WARN  Registered worktree {worktree.pk} at {worktree.worktree_path} is on a session-scoped path "
            f"({reason}) — its checkout is pruned with the session, so unpushed work there survives nowhere. "
            f"Durable root: {durable_checkout_root()}. Fix: t3 <overlay> workspace salvage to recover the work, "
            "then t3 <overlay> workspace relocate."
        )
    return True


def check_worktree_health() -> bool:
    """Every worktree-health check, each evaluated so none masks another.

    An unreadable worktree registry (no DB, a migration mid-flight) WARNs rather
    than failing the doctor run: this check reports on state it reads, so being
    unable to read it is "unverified", never "broken".
    """
    try:
        return all(
            (
                _check_registered_worktrees_are_checkouts(),
                _check_one_worktree_root(),
                _check_no_session_scoped_worktrees(),
            )
        )
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Worktree health UNVERIFIED: the worktree registry could not be read ({exc}).")
        return True


__all__ = ["check_worktree_health"]
