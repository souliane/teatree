"""Orphan auto-isolated worktree env-dir reaping for ``t3 teatree workspace clean-all``.

Its own module so :mod:`teatree.core.management.commands._workspace_cleanup`
stays under the module-health LOC + function caps (mirrors
``_workspace_docker``). A git worktree's auto-isolated env dir
(``~/.local/share/teatree-worktrees/<slug>`` holding a per-worktree
``db.sqlite3`` + ``logs/``) lingers after the checkout is gone; this reaps the
dirs no live ``Worktree`` row references, never one holding a git checkout
(#291, mirroring the #706/#835 data-loss discipline).
"""

import shutil
from pathlib import Path

from teatree import paths
from teatree.core.management.commands._workspace_cleanup import is_clean_ignored
from teatree.core.models import Worktree


def _referenced_isolated_slugs() -> set[str]:
    """Slugs of the auto-isolated env dirs owned by a live ``Worktree`` row.

    A worktree's env dir — the dir holding its DB-backed isolated sqlite DB — is
    named by :func:`paths.isolated_slug` of its on-disk checkout path
    (``extra['worktree_path']``), the same deterministic mapping the resolver
    (:func:`paths.resolve_data_dir`) uses. Hashing every live row's checkout
    through it yields the keep-set, so the reaper agrees with the resolver on
    which dirs are still in use. A row with no recorded checkout path
    contributes nothing — its dir, if any, is indistinguishable from an orphan.
    """
    referenced: set[str] = set()
    for worktree in Worktree.objects.all():
        extra = worktree.extra if isinstance(worktree.extra, dict) else {}
        checkout = str(extra.get("worktree_path", ""))
        if checkout:
            referenced.add(paths.isolated_slug(Path(checkout)))
    return referenced


def _holds_git_checkout(env_dir: Path) -> bool:
    """Whether *env_dir* holds a git checkout — never reap one if so (#291).

    A managed auto-isolated env dir holds only a sqlite DB + ``logs/`` and is
    never a git checkout. A ``.git`` entry — a dir (real repo) or a file (linked
    worktree) — means an unexpected checkout landed here, where uncommitted or
    unpushed work could live. Such a dir is kept defensively, mirroring the
    #706/#835 data-loss discipline: only no-checkout dirs are ever reaped. The
    ``.git`` presence is the precise signal — working-tree state only exists when
    a ``.git`` is present, so this one check covers both "real checkout" and "any
    uncommitted/unpushed work".
    """
    return (env_dir / ".git").exists()


def reap_orphan_isolated_worktree_roots() -> list[str]:
    """Remove DB-unreferenced auto-isolated worktree env dirs left on disk (#291).

    Each git worktree gets an auto-isolated env dir under
    :func:`paths.auto_isolated_worktrees_dir` (``db.sqlite3`` + ``logs/``). When
    the checkout is gone but its env dir lingers, the dir is an orphan: no live
    ``Worktree`` row's checkout (its DB-backed sqlite path) hashes to its slug.

    Reaps only the unreferenced dirs that hold no git checkout — a dir matching
    a live row's slug, a ``clean_ignore`` glob, or any git work is skipped with
    a one-line outcome. Only immediate child *directories* of the root are
    considered; loose files (seed locks) are ignored.
    """
    root = paths.auto_isolated_worktrees_dir()
    if not root.is_dir():
        return []
    referenced = _referenced_isolated_slugs()
    outcomes: list[str] = []
    for env_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        slug = env_dir.name
        if slug in referenced:
            continue
        if is_clean_ignored(slug):
            outcomes.append(f"SKIPPED '{slug}': matches clean_ignore — keeping")
            continue
        if _holds_git_checkout(env_dir):
            outcomes.append(f"SKIPPED '{slug}': holds a git checkout (uncommitted/unpushed work) — keeping")
            continue
        shutil.rmtree(env_dir)
        outcomes.append(f"Removed orphan isolated worktree root: {slug}")
    return outcomes
