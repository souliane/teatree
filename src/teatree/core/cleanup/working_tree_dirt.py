"""Real (non-regenerable) uncommitted-change detection for a worktree.

Shared by the dirty-worktree teardown guard (:mod:`teatree.core.cleanup.cleanup`)
and the analyze-before-wipe done pass (:mod:`teatree.core.worktree.worktree_done`)
so both decide "does this worktree hold real uncommitted work?" identically.

A naive ``git status --porcelain`` over-reports two ways, and BOTH would falsely
refuse a legitimate teardown:

- **Regenerable provisioning artifacts.** Provisioning writes the env cache into
  every worktree, so a porcelain status listing only those is still clean for the
  wipe decision — they are ignored.
- **Dangling-HEAD noise.** A post-merge branch-ref deletion leaves HEAD
  unresolvable, so ``git status`` reports EVERY tracked file as a staged addition.
  That is noise, not real uncommitted work; the working tree is instead diffed
  against the RECOVERED last-HEAD SHA plus an untracked-file scan.

Fails CLOSED: an inconclusive ``git status`` (corrupt index, lock contention) or
an unrecoverable HEAD is treated as dirty so the worktree is KEPT — a guard that
guessed "clean" on an error could let a force-wipe destroy real edits.

Imports ``_EffectiveTarget`` only under :data:`TYPE_CHECKING` so there is no
runtime import cycle with :mod:`teatree.core.cleanup.cleanup`, which imports this
module for its dirty-worktree guard.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.cleanup.cleanup_orphan_ref import classify_orphan_ref
from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME
from teatree.utils import git
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.cleanup.cleanup import _EffectiveTarget

# Regenerable artifacts a "real uncommitted change" probe must ignore: provisioning
# writes the env cache into every worktree, so a porcelain status listing only
# those is still clean for the wipe decision.
_REGENERABLE_WORKTREE_PATHS = (CACHE_FILENAME, f"{CACHE_DIRNAME}/")
_PREVIEW_LIMIT = 3


def _porcelain_path(line: str) -> str:
    """The PATH from one ``git status --porcelain`` line, or ``""`` for a blank line.

    A porcelain line is ``XY PATH`` (a two-column status code + one space + path).
    The status codes are never split from the path by an inner blank, so splitting
    on the first whitespace run yields the path robustly — and, unlike a fixed
    column offset, it survives :func:`teatree.utils.git.run` having stripped the
    leading space of a worktree-only status (e.g. ``" M path"`` → ``"M path"``),
    which would otherwise shift a fixed slice one char into the filename.
    """
    try:
        return line.split(maxsplit=1)[1].strip()
    except IndexError:
        return ""


def real_uncommitted_reasons(wt_path: str, target: "_EffectiveTarget") -> list[str]:
    """Kept-reasons for real (non-regenerable) uncommitted changes; empty when clean.

    Fails CLOSED: an inconclusive ``git status`` (corrupt index, lock contention)
    is treated as dirty so the worktree is kept. A dangling-HEAD worktree (its
    branch ref deleted post-merge) has no resolvable HEAD, so ``git status``
    reports EVERY tracked file as a staged addition — noise, not real uncommitted
    work. Rather than skipping the dirt check entirely there (which would let a
    force-wipe destroy genuine uncommitted follow-up edits), the working tree is
    diffed against the RECOVERED last-HEAD SHA plus an untracked-file scan —
    :func:`_dangling_head_dirty_reasons`.
    """
    if not Path(wt_path).is_dir():
        return []
    if not git.check(repo=wt_path, args=["rev-parse", "--verify", "--quiet", "HEAD"]):
        return _dangling_head_dirty_reasons(wt_path, target)
    try:
        porcelain = git.status_porcelain(wt_path)
    except CommandFailedError as exc:
        return [f"could not read working-tree status ({exc}) — keeping"]
    dirty = [
        path
        for line in porcelain.splitlines()
        if (path := _porcelain_path(line)) and not path.startswith(_REGENERABLE_WORKTREE_PATHS)
    ]
    return _dirt_reasons(dirty)


def _dangling_head_dirty_reasons(wt_path: str, target: "_EffectiveTarget") -> list[str]:
    """Kept-reasons for real uncommitted edits in a dangling-HEAD worktree; empty when clean.

    A post-merge branch-ref deletion leaves HEAD unresolvable, so ``git status``
    is useless (everything reads as a staged add). The recovered last-HEAD SHA is
    the real comparison base: the working tree is diffed against it
    (``git diff --name-only <sha>`` — tracked modifications) plus an untracked-file
    scan (``git ls-files --others --exclude-standard``), ignoring the regenerable
    env cache. Fails CLOSED: an unrecoverable HEAD or an erroring diff keeps the
    worktree rather than letting a force-wipe destroy unexamined edits.
    """
    sha = classify_orphan_ref(target).recovered_sha
    if sha is None:
        return ["could not recover HEAD to check working-tree changes — keeping"]
    try:
        changed = git.run(repo=wt_path, args=["diff", "--name-only", sha])
        untracked = git.run(repo=wt_path, args=["ls-files", "--others", "--exclude-standard"])
    except CommandFailedError as exc:
        return [f"could not diff working tree against recovered HEAD ({exc}) — keeping"]
    dirty = [
        stripped
        for raw in (*changed.splitlines(), *untracked.splitlines())
        if (stripped := raw.strip()) and not stripped.startswith(_REGENERABLE_WORKTREE_PATHS)
    ]
    return _dirt_reasons(dirty)


def _dirt_reasons(dirty: list[str]) -> list[str]:
    """Format a single kept-reason for a non-empty ``dirty`` file list; empty when clean."""
    if not dirty:
        return []
    preview = ", ".join(dirty[:_PREVIEW_LIMIT]) + (", …" if len(dirty) > _PREVIEW_LIMIT else "")
    return [f"{len(dirty)} uncommitted change(s) not on any remote: {preview}"]
