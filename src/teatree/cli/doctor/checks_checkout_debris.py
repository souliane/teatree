"""Untracked debris in a checkout can silently break a repo-root-scanning gate.

``ty-check`` (and any other tool that walks the checkout root rather than a
declared package list) type-checks whatever Python it finds there, tracked or
not. A stray scratch snapshot left outside git — an agent's before/after
capture of ``src/`` that was never cleaned up — is invisible to every
git-scoped review, yet still reddens the NEXT ``t3 tool verify-gates`` run in
that checkout. Found when a periodic architectural review's own
``.review-<n>-scratch/held/`` copy in the main clone broke ``ty-check`` weeks
after it was written, with nothing surfacing the debris in between.

Advisory, never a gate: an operator mid-capture legitimately has untracked
files too, and this only prompts a look, not a fix.
"""

from pathlib import Path

import typer

_LISTED = 10


def check_checkout_untracked_debris() -> bool:
    """WARN when this checkout carries untracked, non-ignored paths.

    Crash-proof and always returns ``True`` — advisory, not a correctness gate.
    """
    try:
        paths = _untracked_paths(_repo_root())
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Checkout-cleanliness check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not paths:
        return True
    shown = ", ".join(paths[:_LISTED])
    more = f" … and {len(paths) - _LISTED} more" if len(paths) > _LISTED else ""
    typer.echo(
        f"WARN  {len(paths)} untracked path(s) sit in this checkout outside git: {shown}{more}. "
        "A repo-root-scanning gate (`ty-check`) can silently start failing on stale content inside "
        "them for the next session that runs `t3 tool verify-gates` here — clean up, or move scratch "
        "output into a worktree of its own."
    )
    return True


def _repo_root() -> Path | None:
    """This checkout's root, or ``None`` when the cwd is not in one."""
    from teatree.utils.git_run import run  # noqa: PLC0415 — deferred: keeps CLI startup light

    top = run(repo=str(Path.cwd()), args=["rev-parse", "--show-toplevel"])
    return Path(top) if top else None


def _untracked_paths(repo_root: Path | None) -> list[str]:
    """Top-level untracked, non-ignored entries — one per stray root item, not per file inside it."""
    if repo_root is None:
        return []
    from teatree.utils.git_status import status_porcelain  # noqa: PLC0415 — deferred: keeps CLI startup light

    out = status_porcelain(str(repo_root))
    if not out:
        return []
    return sorted({line[3:] for line in out.splitlines() if line.startswith("??")})


__all__ = ["check_checkout_untracked_debris"]
