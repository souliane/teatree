"""The pressure loop's heuristic worktree GC — what it may remove, and why not (#4244).

The flag-gated (``allow_destructive_disk``) bottom rung of the disk ladder in
:mod:`teatree.loop.mechanical_resources`. Split out because the GC is its own
concern: enumerating the population, judging each member, and removing the
survivors is a different job from sequencing the ladder.

It reclaimed nothing for its whole life. The enumeration ran ``git worktree
list`` against the worktree ROOT — a directory that CONTAINS worktrees and is
not a repository — so git refused, the refusal became ``[]``, and "I could not
read this" and "there is nothing to reap" were the same value on every tick:
0 enumerated of 167 directories present, against 113 the owning clone reports.
Enumeration is now
:func:`teatree.core.cleanup.checkout_registry.linked_worktree_paths`, which asks
a repository and reports what it could not read as a gap.

HEURISTIC is the operative word, and why the flag guards this and not the
proven-done sweep beside it: clean + fully pushed + untouched is an INFERENCE
that a worktree is finished with, and the same description fits one an agent is
about to come back to. So every judgement is fail-safe to keep, the reason is
recorded rather than swallowed, and a live process inside the directory outranks
every timestamp.

That last guard is only as good as the table behind it, and this module read one
it never checked: an unusable table's empty paths said "nobody is inside" for
every worktree on the box — the #4306 fail-open, live here in any venue without
the host mount. It now refuses the pass instead, and re-establishes the guard
immediately before each removal rather than trusting a survey the intervening
walks have had 34-68 s to invalidate.
"""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from django.utils import timezone

from teatree.config import worktree_root
from teatree.core.cleanup.checkout_registry import linked_worktree_paths, one_spelling_each
from teatree.core.cleanup.disk_usage import dir_size_gb
from teatree.core.cleanup.process_table import ProcessTable, read_process_table
from teatree.loop.dispatch import ActionPayload
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

logger = logging.getLogger(__name__)

#: How long a worktree must sit untouched before the heuristic will consider it.
_DEFAULT_STALE_DAYS = 30


@dataclass(frozen=True, slots=True)
class GcSurvey:
    """What the GC looked at, what it may remove, and what it could not see."""

    candidates: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    considered: int = 0
    refusal: str = ""
    #: The staleness threshold this survey judged with, frozen so the delete-time
    #: re-judgement applies the identical one.
    stale_days: int = _DEFAULT_STALE_DAYS


@dataclass(frozen=True, slots=True)
class GcOutcome:
    """What a collection actually did — what it reclaimed, what the guard stopped, why it refused."""

    reclaimed_gb: float = 0.0
    skipped: tuple[str, ...] = ()
    refusal: str = ""


def survey_worktrees(payload: ActionPayload) -> GcSurvey:
    """Which worktrees are eligible for GC, and the reason each of the rest is kept."""
    stale_days = int(payload.get("worktree_stale_days", _DEFAULT_STALE_DAYS))
    cap = int(payload.get("max_worktree_gc_per_tick", 3))
    cwd = safe_cwd()
    table = read_process_table()
    if refusal := table.refuse_reason():
        return GcSurvey(refusal=refusal, stale_days=stale_days)
    enumeration = linked_worktree_paths(worktree_root())
    candidates: list[str] = []
    kept: list[str] = []
    for wt in one_spelling_each(enumeration.paths):
        reason = keep_reason(wt, stale_days=stale_days, cwd=cwd, table=table)
        if reason:
            kept.append(f"{wt}: {reason}")
        elif len(candidates) < cap:
            candidates.append(str(wt))
        else:
            kept.append(f"{wt}: over the {cap}-per-tick cap, deferred to the next pass")
    return GcSurvey(tuple(candidates), tuple(kept), enumeration.gaps, len(enumeration.paths), stale_days=stale_days)


def collect(survey: GcSurvey) -> GcOutcome:
    """Remove every candidate the guard still allows, re-judged at the moment of removal."""
    table = read_process_table()
    if refusal := table.refuse_reason():
        return GcOutcome(refusal=f"the process table stopped answering after the survey — {refusal}")
    cwd = safe_cwd()
    reclaimed = 0.0
    skipped: list[str] = []
    for wt in survey.candidates:
        reason = keep_reason(Path(wt), stale_days=survey.stale_days, cwd=cwd, table=table)
        if reason:
            skipped.append(f"{wt}: {reason} since the survey")
            continue
        size_gb = dir_size_gb(Path(wt))
        if remove_worktree(Path(wt)):
            reclaimed += size_gb
    return GcOutcome(reclaimed, tuple(skipped))


def keep_reason(wt: Path, *, stale_days: int, cwd: Path | None, table: ProcessTable) -> str:
    """Why *wt* survives this pass, or ``""`` when nothing keeps it."""
    return _in_use_reason(wt, cwd=cwd, table=table) or _unsettled_reason(wt, stale_days=stale_days)


def _in_use_reason(wt: Path, *, cwd: Path | None, table: ProcessTable) -> str:
    """Somebody is working in it — the guard the timestamp heuristic cannot supply."""
    if cwd is not None and is_within(cwd, wt):
        return "this process is working inside it"
    if table.holds(wt):
        return "a live process is working inside it"
    return ""


def _unsettled_reason(wt: Path, *, stale_days: int) -> str:
    """It still holds something, or git would not confirm that it does not."""
    if not wt.is_dir():
        return "its directory is gone"
    if git_dirty(wt):
        return "it has uncommitted changes, or git would not say"
    if git_ahead_of_upstream(wt):
        return "it holds commits no remote has, or git would not say"
    if not is_stale(wt, stale_days=stale_days):
        return f"it was touched within the last {stale_days}d"
    return ""


def git_dirty(wt: Path) -> bool:
    out = _git(wt, "status", "--porcelain")
    if out is None:
        return True  # can't tell → treat as dirty (skip)
    return bool(out.strip())


def git_ahead_of_upstream(wt: Path) -> bool:
    out = _git(wt, "log", "@{u}..", "--oneline")
    if out is None:
        return True  # no upstream / can't tell → treat as ahead (skip)
    return bool(out.strip())


def is_stale(wt: Path, *, stale_days: int) -> bool:
    try:
        mtime = wt.stat().st_mtime
    except OSError:
        return False
    age_days = (timezone.now().timestamp() - mtime) / 86400
    return age_days >= stale_days


def remove_worktree(wt: Path) -> bool:
    # ``-C <wt>`` resolves the worktree's gitdir before removal, so the call
    # works even though the worktree's parent dir is the (non-repo) workspace
    # root. Running from the parent would ``fatal: not a git repository``.
    return _git(wt, "worktree", "remove", str(wt)) is not None


def safe_cwd() -> Path | None:
    try:
        return Path.cwd().resolve()
    except OSError:
        return None


def is_within(child: Path, ancestor: Path) -> bool:
    """True iff *child* is the same as or nested under *ancestor* (resolved)."""
    try:
        resolved = ancestor.resolve()
    except OSError:
        return False
    return resolved == child or resolved in child.parents


def _git(cwd: Path, *args: str) -> str | None:
    """Run a git command in *cwd*; ``None`` on any failure, which every caller keeps on."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = run_allowed_to_fail([git, *args], expected_codes=None, cwd=cwd, timeout=60)
    except (OSError, CommandFailedError):
        return None
    except Exception:
        logger.exception("worktree_gc: git %s raised", args[0] if args else "")
        return None
    return result.stdout if result.returncode == 0 else None


__all__ = ["GcOutcome", "GcSurvey", "collect", "keep_reason", "survey_worktrees"]
