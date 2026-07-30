"""Source-clone resolution shared by provisioning, cleanup, orphan-guard, reconcile.

Lives in ``teatree.core`` rather than ``teatree.core.runners`` because importing
``runners`` triggers ``runners.__init__`` which pulls in ``cleanup`` (via
``teardown``) — a circular import the moment ``cleanup`` itself wants to
resolve a clone.
"""

import logging
from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.worktree.worktree_roots import CheckoutState, probe_checkout

logger = logging.getLogger(__name__)


def find_clone_path(workspace: Path, repo_name: str) -> Path | None:
    """Resolve ``repo_name`` to an actual git clone under ``workspace``.

    Tries the literal path first (``workspace / repo_name``) so explicit
    ``souliane/teatree``-style entries keep working. If that's not a git
    checkout, scans one level deep — ``workspace / */basename`` — so a bare
    ``teatree`` from ``--repos teatree`` finds the namespaced clone at
    ``workspace/souliane/teatree``. Returns ``None`` when no match exists.
    Logs a warning when more than one match is found and picks the first
    (alphabetic) so the operator can spot basename collisions in the logs.

    A non-existent ``workspace`` resolves to ``None`` (no clone), never a crash:
    the per-overlay ``workspace_dir`` default (``~/workspace/t3-workspaces/<overlay>/``)
    need not exist yet on a fresh setup, and ``iterdir()`` would raise
    ``FileNotFoundError`` on the one-level scan otherwise.
    """
    literal = workspace / repo_name
    if (literal / ".git").is_dir():
        return literal

    if not workspace.is_dir():
        return None

    basename = Path(repo_name).name
    matches: list[Path] = []
    for entry in sorted(workspace.iterdir()):
        if not entry.is_dir() or entry == literal:
            continue
        candidate = entry / basename
        if (candidate / ".git").is_dir():
            matches.append(candidate)

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple clones match %r under %s; picking %s. Pass --repos with the namespace prefix to disambiguate.",
            repo_name,
            workspace,
            matches[0],
        )
    return matches[0]


def stored_clone_path(worktree: Worktree) -> Path | None:
    """``worktree.extra['clone_path']``, but only while it still resolves as a checkout.

    The recorded path is a claim, not a fact: a deploy that relocates (or a
    hand-moved clone) leaves the row pointing at nothing, and every git probe run
    against that path answers "could not read" — which the redundancy layers then
    render as an empty unique-commit list, indistinguishable from a branch proven
    to hold nothing. Positive proof is required, so an ``INCONCLUSIVE`` probe
    falls through to a fresh scan instead of being trusted.
    """
    stored = (worktree.extra or {}).get("clone_path", "")
    if not stored:
        return None
    return Path(stored) if probe_checkout(Path(stored)) is CheckoutState.CHECKOUT else None


def resolve_clone_path(workspace: Path, worktree: Worktree) -> Path | None:
    """Return the source clone path for *worktree*, with namespace fallback.

    Prefers a :func:`stored_clone_path` that is still a live checkout; a stale or
    unverifiable stored value falls through to a fresh :func:`find_clone_path`
    scan, exactly like a row that never carried the field. ``None`` means no clone
    exists anywhere — callers read that as unverifiable and keep, never as
    "nothing here to lose".
    """
    stored = stored_clone_path(worktree)
    if stored is not None:
        return stored
    return find_clone_path(workspace, worktree.repo_path)


def repair_stale_clone_path(workspace: Path, worktree: Worktree) -> Path | None:
    """Rewrite a stale ``extra['clone_path']`` to the clone that exists; ``None`` when untouched.

    Only ever moves the row toward the truth: a stored path the checkout probe
    confirms is left alone, and a scan that finds nothing leaves the stale value
    in place as a breadcrumb rather than blanking the only record of where the
    clone used to be.
    """
    if stored_clone_path(worktree) is not None:
        return None
    found = find_clone_path(workspace, worktree.repo_path)
    if found is None or str(found) == (worktree.extra or {}).get("clone_path", ""):
        return None
    worktree.extra = {**(worktree.extra or {}), "clone_path": str(found)}
    worktree.save(update_fields=["extra"])
    return found
