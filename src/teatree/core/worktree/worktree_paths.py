"""Single source of truth for worktree path-layout + symlink-tolerant matching.

Finding 18 (loops/worktree audit): the canonical ``<workspace>/<branch>/<repo-leaf>``
layout and the symlink-tolerant path matcher were each reimplemented across
``provision``, ``cleanup``, ``reconcile``, ``worktree_collision``, the orphan-worktree
reaper and ``resolve``. This module owns both so every site shares one
implementation rather than drifting copies.

Pure (``pathlib`` only): callers pass ``workspace`` / ``branch`` / ``repo``
explicitly, so the helpers stay free of config + model imports and sit at the
bottom of the dependency graph. The branch source legitimately differs per
caller (``ticket.extra['branch']`` for the provision/intake path that names the
shared ticket dir; the per-repo ``Worktree.branch`` for the cleanup fallback,
whose ticket may carry no ``extra['branch']`` at all), so the layout JOIN — not
the branch — is what these helpers centralise.
"""

from pathlib import Path


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


def _candidate_paths(path: str) -> list[str]:
    """Return de-duplicated list of path variants to try for DB lookups.

    On macOS, ``/var`` is a symlink to ``/private/var``, so a path stored
    as ``/var/folders/…`` won't match ``/private/var/folders/…`` (and vice
    versa).  We try the original, the resolved form, and the ``/private``
    prefix stripped/added variants.
    """
    candidates: list[str] = [path]
    resolved = str(Path(path).resolve())
    if resolved != path:
        candidates.append(resolved)
    # macOS: /private/var ↔ /var, /private/tmp ↔ /tmp, /private/etc ↔ /etc
    if path.startswith("/private/"):
        candidates.append(path.removeprefix("/private"))
    else:
        prefixed = "/private" + path
        if Path(prefixed).exists():
            candidates.append(prefixed)
    return candidates


def paths_match(a: str | Path, b: str | Path) -> bool:
    """Whether *a* and *b* refer to the same location, symlink-tolerant.

    The pairwise form of the matcher the DB-lookup sites in :mod:`teatree.core.resolve`
    already use via ``filter(extra__worktree_path__in=_candidate_paths(...))``. Two
    paths match when ANY of their :func:`_candidate_paths` variants coincide, so a
    ``/var`` path matches its ``/private/var`` twin (and a resolved symlink matches
    its source). Routing the on-disk pairwise comparisons through it — the
    ``reconcile`` stale-dir scan, ``worktree_collision`` own-dir exclusion, the
    orphan-worktree DB-tracked check, the idle-stack active-CWD check — gives every
    site the same symlink tolerance the DB matcher has, instead of a bare
    ``.resolve() ==`` that misses the ``/private`` literal variants.
    """
    return bool(set(_candidate_paths(str(a))) & set(_candidate_paths(str(b))))
