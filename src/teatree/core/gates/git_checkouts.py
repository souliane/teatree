"""Discover every git checkout teatree actually commits and pushes from.

The installed clone is only one of them. Work happens in worktrees whose git
common dir belongs to a DIFFERENT clone — on a containerized deploy the CLI runs
from the container clone while every commit and push comes from the host
checkout. Anything that verifies or repairs a per-checkout property (git hooks
first of all) has to see that whole set, or it draws a green verdict from the one
clone nobody pushes from.

Scope is the checkouts no other mechanism covers: the installed clone, every
checkout under the auto-isolated worktrees root — including ad-hoc ones created
by a bare ``git worktree add``, which is precisely the population that exposed
the unprotected host clone — and the clone owning each. A teatree-provisioned
worktree already gets its hooks from ``worktree provision``.

Django-free by construction (stdlib plus :mod:`teatree.paths`), so the setup and
doctor paths can call it before ``ensure_django``. Each source is independently
crash-proof: an absent root or a vanished path drops that source rather than
aborting discovery.
"""

from collections.abc import Iterator
from pathlib import Path

from teatree.paths import auto_isolated_worktrees_dir
from teatree.utils.run import CommandFailedError, run_allowed_to_fail


def _installed_clone() -> Path | None:
    """The clone this ``teatree`` package is installed from, or ``None`` when packaged."""
    import teatree  # noqa: PLC0415 — deferred: the package cannot import itself at module scope

    repo = Path(teatree.__file__).resolve().parents[2]
    return repo if (repo / ".git").exists() else None


def _isolated_worktrees() -> Iterator[Path]:
    """Checkouts under the auto-isolated worktrees root, tracked or not."""
    try:
        root = auto_isolated_worktrees_dir()
        children = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        return
    for child in children:
        if (child / ".git").exists():
            yield child


def owning_clone(checkout: Path) -> Path | None:
    """The clone whose git dir *checkout* commits through, or ``None`` if unresolvable.

    For a linked worktree this is the main clone behind its common dir — the path
    an operator has to repair, and the one worth naming in a report.
    """
    try:
        result = run_allowed_to_fail(
            ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            expected_codes=None,
        )
    except (OSError, CommandFailedError):
        return None
    common = result.stdout.strip()
    if result.returncode != 0 or not common:
        return None
    parent = Path(common).parent
    return parent if parent.is_dir() else None


def discover_checkouts() -> list[Path]:
    """Every checkout teatree commits from, deduped, most-authoritative first.

    Clones lead worktrees so a report names the clone an operator repairs rather
    than whichever worktree happened to surface it.
    """
    worktrees = list(_isolated_worktrees())
    clones = [clone for checkout in worktrees if (clone := owning_clone(checkout)) is not None]

    ordered: list[Path] = []
    installed = _installed_clone()
    if installed is not None:
        ordered.append(installed)
    ordered.extend(clones)
    ordered.extend(worktrees)

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in ordered:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


__all__ = ["discover_checkouts", "owning_clone"]
