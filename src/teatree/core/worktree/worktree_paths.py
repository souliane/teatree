"""Single source of truth for worktree path-layout + symlink-tolerant matching.

Finding 18 (loops/worktree audit): the canonical ``<workspace>/<branch>/<repo-leaf>``
layout and the symlink-tolerant path matcher were each reimplemented across
``provision``, ``cleanup``, ``reconcile``, ``worktree_collision``, the orphan-worktree
reaper and ``resolve``. This module owns both so every site shares one
implementation rather than drifting copies.

Django-free, with mount spellings from ``teatree.mount_identity``: callers pass ``workspace`` / ``branch`` / ``repo``
explicitly, so the helpers stay free of config + model imports and sit at the
bottom of the dependency graph. The branch source legitimately differs per
caller (``ticket.extra['branch']`` for the provision/intake path that names the
shared ticket dir; the per-repo ``Worktree.branch`` for the cleanup fallback,
whose ticket may carry no ``extra['branch']`` at all), so the layout JOIN — not
the branch — is what these helpers centralise.
"""

from collections.abc import Iterable
from pathlib import Path

from teatree.mount_identity import MountTable, parsed_mount_table, read_mount_table


def ticket_dir_for(workspace: Path, branch: str) -> Path:
    """Canonical per-ticket workspace dir holding each repo's worktree as a sibling.

    ``<workspace>/<branch>`` — the directory ``workspace ticket`` provisions every
    affected repo into (each repo materialises at ``<ticket-dir>/<repo-leaf>``),
    even when the repos sit on split per-repo branches.
    """
    return workspace / branch


def worktree_dir_for(workspace: Path, branch: str, repo: str) -> Path:
    """Canonical on-disk worktree path: ``<workspace>/<branch>/<repo-leaf>``.

    ``repo`` may be a bare name or an ``owner/repo`` slug; only its leaf names the
    on-disk dir, mirroring ``WorktreeProvisioner._create``.
    """
    return ticket_dir_for(workspace, branch) / Path(repo).name


def _local_paths(path: str) -> tuple[list[str], str]:
    candidates: list[str] = [path]
    resolved = str(Path(path).resolve())
    if resolved != path:
        candidates.append(resolved)
    return list(dict.fromkeys(candidates)), resolved


def _candidate_paths(path: str, *, mount_table: MountTable | None = None) -> list[str]:
    """Return literal, resolved and verified mount spellings for DB lookups."""
    candidates, resolved = _local_paths(path)
    table = mount_table if mount_table is not None else parsed_mount_table(read_mount_table())
    candidates.extend(table.spellings(path, resolved=resolved))
    return list(dict.fromkeys(candidates))


def path_spellings(paths: Iterable[str | Path]) -> set[str]:
    table = parsed_mount_table(read_mount_table())
    return {spelling for path in paths for spelling in _candidate_paths(str(path), mount_table=table)}


def path_is_within(
    path: str | Path,
    root: str | Path,
    *,
    mount_table: MountTable | None = None,
) -> bool:
    if Path(path).is_relative_to(root):
        return True
    table = mount_table if mount_table is not None else parsed_mount_table(read_mount_table())
    resolved = str(Path(path).resolve())
    local_roots, _ = _local_paths(str(root))
    return any(
        Path(alias).is_relative_to(ancestor)
        for alias in table.spellings(str(path), resolved=resolved)
        if alias not in {str(path), resolved}
        for ancestor in local_roots
    )


def paths_match(a: str | Path, b: str | Path) -> bool:
    """Whether *a* and *b* refer to the same location, symlink-tolerant.

    The pairwise form of the matcher the DB-lookup sites in :mod:`teatree.core.intake.resolve`
    already use via ``filter(extra__worktree_path__in=_candidate_paths(...))``. Two
    paths match when their candidate spellings coincide, so a resolved symlink matches
    its source. Routing the on-disk pairwise comparisons through it — the
    ``reconcile`` stale-dir scan and ``worktree_collision`` own-dir exclusion
    use the DB matcher's symlink
    and mount tolerance. :func:`path_is_within` handles descendants, including
    the idle-stack active-CWD check.
    """
    table = parsed_mount_table(read_mount_table())
    return not set(_candidate_paths(str(a), mount_table=table)).isdisjoint(_candidate_paths(str(b), mount_table=table))
