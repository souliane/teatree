"""Source-clone resolution shared by provisioning, cleanup, orphan-guard, reconcile.

Lives in ``teatree.core`` rather than ``teatree.core.runners`` because importing
``runners`` triggers ``runners.__init__`` which pulls in ``cleanup`` (via
``teardown``) — a circular import the moment ``cleanup`` itself wants to
resolve a clone.
"""

import logging
from pathlib import Path

from teatree.core.models import Worktree

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
    """
    literal = workspace / repo_name
    if (literal / ".git").is_dir():
        return literal

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


def resolve_clone_path(workspace: Path, worktree: Worktree) -> Path | None:
    """Return the source clone path for *worktree*, with namespace fallback.

    Prefers ``worktree.extra['clone_path']`` (set at provision time when the
    namespace-aware lookup was used). Falls back to a fresh
    :func:`find_clone_path` scan for legacy rows that pre-date the field.
    Returns ``None`` when no clone exists anywhere.
    """
    stored = (worktree.extra or {}).get("clone_path", "")
    if stored:
        return Path(stored)
    return find_clone_path(workspace, worktree.repo_path)
