"""Registered worktrees must be real git checkouts under one root (#3583).

Two silent decays this surfaces. A registered ``Worktree`` row whose dir no
longer resolves as a git checkout is dead — every git-driven pass over it (hook
installation, branch classification, teardown) fails with a WARN nobody reads,
and the row keeps the dir alive in every listing. And a worktree living outside
the canonical worktree root splits the namespace the reaper and this doctor
scan, so broken checkouts accumulate in the half nothing sweeps.
"""

from pathlib import Path

import typer


def _check_registered_worktrees_are_checkouts() -> bool:
    """FAIL on a registered worktree dir that ``git rev-parse`` cannot resolve.

    A missing dir is NOT a failure here — that is an ordinary reaped worktree
    whose row the done-reaper releases. The failure is a dir that EXISTS but is
    not a checkout: the state that produced the repeating "is not a git checkout"
    setup WARNs.
    """
    from teatree.core.models import Worktree  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.core.worktree.worktree_roots import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        resolves_as_git_checkout,
    )

    broken = [
        worktree
        for worktree in Worktree.objects.all()
        if worktree.worktree_path
        and Path(worktree.worktree_path).is_dir()
        and not resolves_as_git_checkout(Path(worktree.worktree_path))
    ]
    for worktree in broken:
        typer.echo(
            f"FAIL  Registered worktree {worktree.pk} at {worktree.worktree_path} is not a git checkout "
            "(git rev-parse fails) — every git-driven pass over it silently no-ops. "
            "Fix: t3 <overlay> workspace clean-all (the broken-worktree reaper drops it)."
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
