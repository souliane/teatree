"""Filesystem-evidence double-dispatch guard at the worktree-provisioning seam (#2217).

The #2104 / #2217 delivery-ownership lease is a DB signal: it can only protect a
unit the DB knows about. When the DB has NO ticket for an issue — the loop's
issue-scanner raced the ``workspace ticket`` call, or a ticket write was lost —
the lease is blind, and two agents can provision a worktree for the same issue.

This guard works from filesystem evidence alone. The worktree directory for
issue ``N`` is deterministically named ``<N>-<slug>`` under ``$T3_WORKSPACE_DIR``
(``build_branch_name``: ``<ticket_number>-<slugified-title>``), so globbing
``<N>-*`` finds any existing worktree for the same issue regardless of the DB
state. A ``Worktree`` row whose materialised path sits under such a directory is
the corroborating DB signal. Both the loop-dispatch path and the hand-dispatch
path provision through the same ``workspace ticket`` seam, so one check covers
both.

The guard runs inside the ``workspace ticket`` transaction, keyed on
``Ticket.ticket_number`` (the same value that names the branch dir), so a refusal
ROLLS BACK the freshly-created ticket and leaves zero DB trace — no stranded
ticket for a unit someone else is already provisioning.
"""

from pathlib import Path

from teatree.core.models import Worktree


def _issue_dir_glob(issue_number: str) -> str:
    """Glob pattern matching every worktree dir for ``issue_number`` (``<N>-*``)."""
    return f"{issue_number}-*"


def find_foreign_issue_worktrees(
    issue_number: str,
    *,
    own_path: Path,
    workspace_dir: Path,
) -> list[Path]:
    """Return existing worktree dirs for ``issue_number`` that are NOT ``own_path``.

    A non-empty result means someone may already be working issue ``issue_number``
    at a different path — the caller refuses unless the operator passes
    ``--take-over``. ``own_path`` (the directory THIS ticket would use) is always
    excluded so re-provisioning a ticket's own existing worktree is idempotent.

    Evidence is unioned from two sources, both keyed on the issue number.
    Filesystem: directories matching ``<N>-*`` directly under ``workspace_dir``
    — the primary signal, present even when the DB has no ticket (the exact
    blind spot the DB lease cannot cover). Worktree rows: ``Worktree`` rows
    whose materialised ``worktree_path`` lives inside a ``<N>-*`` directory —
    corroborates the filesystem and catches a row whose on-disk dir was pruned
    but the claim still stands.

    The ``<N>-*`` / ``Worktree`` keying is deliberately overlay-agnostic: it
    keys purely on the issue number, so a cross-overlay clash on the same issue
    number is a rare false-positive that ``--take-over`` covers.
    """
    own = own_path.resolve()
    foreign: dict[Path, None] = {}

    if workspace_dir.is_dir():
        for candidate in sorted(workspace_dir.glob(_issue_dir_glob(issue_number))):
            if candidate.is_dir() and candidate.resolve() != own:
                foreign[candidate.resolve()] = None

    prefix = f"{issue_number}-"
    for row in Worktree.objects.exclude(extra__worktree_path__isnull=True):
        raw = row.worktree_path
        if not raw:
            continue
        issue_dir = _issue_dir_root(Path(raw).resolve(), workspace_dir.resolve())
        if issue_dir is not None and issue_dir.name.startswith(prefix) and issue_dir != own:
            foreign[issue_dir] = None

    return list(foreign)


def _issue_dir_root(worktree_path: Path, workspace_dir: Path) -> Path | None:
    """Return the ``<N>-<slug>`` directory directly under ``workspace_dir`` that contains ``worktree_path``.

    A worktree's path is ``$T3_WORKSPACE_DIR/<N>-<slug>/<repo>`` (multi-repo) or
    ``$T3_WORKSPACE_DIR/<N>-<slug>`` itself. Walk up to the first ancestor whose
    parent is ``workspace_dir`` — that ancestor is the issue directory. Returns
    ``None`` when ``worktree_path`` is not under ``workspace_dir`` at all.
    """
    for ancestor in (worktree_path, *worktree_path.parents):
        if ancestor.parent == workspace_dir:
            return ancestor
    return None
