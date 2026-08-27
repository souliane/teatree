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

Failing closed is not the same finding as finding dirt, and
:class:`WorkingTreeDirt` keeps the two apart: ``proven`` is ``False`` exactly when
the reasons describe a probe that could not answer rather than files that are
modified. Both keep the worktree; only one of them is evidence, and a caller that
renders them identically tells its operator about uncommitted work nothing saw.

Imports ``_EffectiveTarget`` only under :data:`TYPE_CHECKING` so there is no
runtime import cycle with :mod:`teatree.core.cleanup.cleanup`, which imports this
module for its dirty-worktree guard.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.cleanup.cleanup_orphan_ref import classify_orphan_ref
from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME
from teatree.utils import git
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.cleanup.cleanup import _EffectiveTarget

# The ONLY paths a "real uncommitted change" probe may ignore: the regenerable
# env cache provisioning writes into every worktree, plus the fixed set of
# known-ephemeral orchestration scratch. Anything unrecognised is REAL work —
# and `.claude/` itself is NEVER debris (product repos track real skills and
# settings there); only its `worktrees/` run scratch is.
ORCHESTRATION_DEBRIS_PREFIXES = (
    CACHE_FILENAME,
    f"{CACHE_DIRNAME}/",
    ".claude/worktrees/",
    ".secfix/",
    ".fix-bug/",
    ".worker-status/",
    ".claude-prompt",
    ".commit-message.txt",
)
_PREVIEW_LIMIT = 3


def is_orchestration_debris(path: str) -> bool:
    return path.startswith(ORCHESTRATION_DEBRIS_PREFIXES)


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


@dataclass(frozen=True, slots=True)
class WorkingTreeDirt:
    """What the dirt probe found — and whether it found anything at all.

    ``reasons`` is what keeps the worktree, empty when nothing does. ``proven``
    says which kind of finding it is: ``True`` for an answered probe (the reasons,
    if any, name modified files), ``False`` for one that could not answer, where
    the reasons name the obstacle instead. A ``False`` verdict is never evidence of
    uncommitted work, only the absence of evidence either way.

    ``paths`` names the modified files themselves — the same set the reasons
    summarise in prose, kept machine-readable because the structured cleanup
    record hands them to a judgment skill that must decide what to salvage. It is
    empty for an unanswered probe, where no file was proven modified.
    """

    reasons: tuple[str, ...]
    proven: bool
    paths: tuple[str, ...] = ()


def real_uncommitted_reasons(wt_path: str, target: "_EffectiveTarget") -> list[str]:
    """Kept-reasons for real (non-regenerable) uncommitted changes; empty when clean.

    The reasons alone, for callers that keep the worktree on any of them and do not
    distinguish proven dirt from an unanswerable probe. Callers that report the
    reason to an operator want :func:`working_tree_dirt`.
    """
    return list(working_tree_dirt(wt_path, target).reasons)


def working_tree_dirt(wt_path: str, target: "_EffectiveTarget") -> WorkingTreeDirt:
    """The full dirt verdict for *wt_path* — reasons plus whether the probe answered.

    Fails CLOSED: an inconclusive ``git status`` (corrupt index, lock contention)
    keeps the worktree, reported as unproven. A dangling-HEAD worktree (its
    branch ref deleted post-merge) has no resolvable HEAD, so ``git status``
    reports EVERY tracked file as a staged addition — noise, not real uncommitted
    work. Rather than skipping the dirt check entirely there (which would let a
    force-wipe destroy genuine uncommitted follow-up edits), the working tree is
    diffed against the RECOVERED last-HEAD SHA plus an untracked-file scan —
    :func:`_dangling_head_dirt`.
    """
    if not Path(wt_path).is_dir():
        return WorkingTreeDirt(reasons=(), proven=True)
    if not git.check(repo=wt_path, args=["rev-parse", "--verify", "--quiet", "HEAD"]):
        return _dangling_head_dirt(wt_path, target)
    try:
        # ``-uall`` lists untracked FILES, never a collapsed ``dir/`` entry — a
        # collapsed ``.claude/`` is undecidable between debris scratch and a real
        # authored skill, and undecidable must read REAL.
        porcelain = git.run_strict(repo=wt_path, args=["status", "--porcelain", "--untracked-files=all"])
        diff_head = git.run_strict(repo=wt_path, args=["diff", "HEAD", "--name-only"])
    except CommandFailedError as exc:
        return WorkingTreeDirt(reasons=(f"could not read working-tree status ({exc}) — keeping",), proven=False)
    candidates = [_porcelain_path(line) for line in porcelain.splitlines()]
    candidates.extend(line.strip() for line in diff_head.splitlines())
    dirty = [path for path in dict.fromkeys(candidates) if path and not is_orchestration_debris(path)]
    return _dirt_verdict(dirty)


def _dangling_head_dirt(wt_path: str, target: "_EffectiveTarget") -> WorkingTreeDirt:
    """The dirt verdict for a dangling-HEAD worktree, diffed against the recovered SHA.

    A post-merge branch-ref deletion leaves HEAD unresolvable, so ``git status``
    is useless (everything reads as a staged add). The recovered last-HEAD SHA is
    the real comparison base: the working tree is diffed against it
    (``git diff --name-only <sha>`` — tracked modifications) plus an untracked-file
    scan (``git ls-files --others --exclude-standard``), ignoring the regenerable
    env cache. Fails CLOSED: an unrecoverable HEAD or an erroring diff keeps the
    worktree — as UNPROVEN — rather than letting a force-wipe destroy unexamined
    edits.
    """
    sha = classify_orphan_ref(target).recovered_sha
    if sha is None:
        return WorkingTreeDirt(
            reasons=("could not recover HEAD to check working-tree changes — keeping",), proven=False
        )
    try:
        changed = git.run_strict(repo=wt_path, args=["diff", "--name-only", sha])
        untracked = git.run_strict(repo=wt_path, args=["ls-files", "--others", "--exclude-standard"])
    except CommandFailedError as exc:
        return WorkingTreeDirt(
            reasons=(f"could not diff working tree against recovered HEAD ({exc}) — keeping",), proven=False
        )
    dirty = [
        stripped
        for raw in (*changed.splitlines(), *untracked.splitlines())
        if (stripped := raw.strip()) and not is_orchestration_debris(stripped)
    ]
    return _dirt_verdict(dirty)


def _dirt_verdict(dirty: list[str]) -> WorkingTreeDirt:
    """The answered-probe verdict for a list of genuinely modified paths."""
    if not dirty:
        return WorkingTreeDirt(reasons=(), proven=True)
    preview = ", ".join(dirty[:_PREVIEW_LIMIT]) + (", …" if len(dirty) > _PREVIEW_LIMIT else "")
    return WorkingTreeDirt(
        reasons=(f"{len(dirty)} uncommitted change(s) not on any remote: {preview}",),
        proven=True,
        paths=tuple(dirty),
    )
