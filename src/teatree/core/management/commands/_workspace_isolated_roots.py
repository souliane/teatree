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
from teatree.core.gates.idle_stack import worktree_protects_against_reap
from teatree.core.management.commands._workspace_cleanup import is_clean_ignored
from teatree.core.models import Worktree


def _has_unmappable_live_worktree() -> bool:
    """True iff a live worktree row lacks a recorded checkout path (#291 data-loss).

    A live worktree WITH a checkout path already contributes its slug to the
    keep-set (:func:`_referenced_isolated_slugs`), so its env dir is kept. But a
    live worktree whose canonical row LOST its ``worktree_path`` (the stale-row
    class the resolver tolerates) cannot be hashed to a slug, so its in-use
    isolated DB looks like an orphan. When any such row exists, no unreferenced
    dir can be proven dead — fail safe and keep them all rather than reap a live
    isolated DB out from under a mid-task agent.

    "Live" is the shared :func:`worktree_protects_against_reap` predicate — a live
    session, an active/claimed task, an external-delivery lease, a recent E2E run,
    or an explicit pin — so this reaper never protects LESS than the reversible
    idle-stack reaper.
    """
    for worktree in Worktree.objects.select_related("ticket"):
        extra = worktree.extra if isinstance(worktree.extra, dict) else {}
        if not str(extra.get("worktree_path", "")) and worktree_protects_against_reap(worktree) is not None:
            return True
    return False


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

    Liveness guard (#291/#2243): when a BUSY worktree row has no recorded
    checkout path (:func:`_has_unmappable_live_worktree`), its in-use isolated DB
    cannot be mapped to a slug, so no unreferenced dir can be proven dead. Every
    such dir is then KEPT — fail safe rather than reap a live isolated DB out
    from under a mid-task agent.
    """
    root = paths.auto_isolated_worktrees_dir()
    if not root.is_dir():
        return []
    referenced = _referenced_isolated_slugs()
    keep_unmappable_live = _has_unmappable_live_worktree()
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
        if keep_unmappable_live:
            outcomes.append(
                f"SKIPPED '{slug}': a live worktree has no recorded checkout path "
                "— cannot prove this env dir is orphan, keeping (live work)"
            )
            continue
        shutil.rmtree(env_dir)
        outcomes.append(f"Removed orphan isolated worktree root: {slug}")
    return outcomes
