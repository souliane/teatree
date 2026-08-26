"""Deregister the cold-review checkouts whose directories the OS already took (#4576).

``add_review_worktree_at_head`` mkdtemps a detached ``t3-review-*`` checkout under
the system temp dir; ``t3 review checkout`` prints its path and exits, so the only
thing that can remove it is a reviewer running as a SEPARATE process. One that dies, times out,
or simply forgets leaves the registration behind, the OS reaps the directory, and
three separate guards then keep the corpse forever: the clone-wide
``git worktree prune`` is refused because the temp root lies outside the venue's
provisioning root, the raw-orphan reaper's absent-dir probe fails closed as "unpushed
work", and a bare ``git worktree lock`` — which the prune's own refusal message
advises — makes the entry invisible to ``prunable`` and immune to ``remove --force``.

This sweep is deliberately NARROW, and per-entry rather than clone-wide, so it needs
none of those gates relaxed. What it may deregister is a triple, and the lock is not
part of it: the basename carries teatree's own mkdtemp prefix, the registration is
DETACHED, and this venue can see the directory is gone (absent with a readable
parent — never merely unresolvable). A directory that is still there is untouched
whatever its lock says, which is what protects a deliberate operator claim.

The residual risk the triple leaves is a review created in ANOTHER venue whose temp
dir this one cannot see, and the age floor bounds it: past the floor that review has
finished or died either way, and a review checkout is detached at an already-pushed
head with no write tools granted to the phase, so the worst a wrong deregistration
can cost is a re-checkout. It can never cost work — which is the whole reason this
may act where the general-purpose reapers must not.
"""

import logging
import time
from datetime import timedelta
from pathlib import Path

from teatree.core.worktree.venue import VenueObservation, observe
from teatree.utils import git
from teatree.utils.review_checkout import REVIEW_WORKTREE_PREFIX

logger = logging.getLogger(__name__)

#: Far beyond any cold review, far below the days a leak survives. Not a config row:
#: the only value an operator could pick is a smaller one, which buys nothing and
#: races a live review.
REVIEW_REGISTRATION_TTL = timedelta(hours=6)


def _registration_mtimes(repo: str) -> dict[Path, float]:
    """Each registered checkout's admin ``gitdir`` mtime — git's own freshness signal.

    Keyed by the path the ``gitdir`` file NAMES rather than by the admin dir's
    basename: git disambiguates a colliding basename with a suffix, so a basename key
    would hand one registration another's age.
    """
    common = git.git_common_dir(repo)
    if common is None:
        return {}
    mtimes: dict[Path, float] = {}
    try:
        entries = sorted((common / "worktrees").iterdir())
    except OSError:
        return {}
    for entry in entries:
        try:
            named = Path((entry / "gitdir").read_text(encoding="utf-8").strip())
            mtimes[named.parent] = (entry / "gitdir").stat().st_mtime
        except OSError:
            continue
    return mtimes


def is_review_checkout(record: git.WorktreeRecord) -> bool:
    """Whether *record* is one of teatree's own ephemeral review checkouts.

    Both halves are load-bearing: an operator's hand-made worktree can carry the
    prefix, but a review checkout is created ``--detach`` at ``FETCH_HEAD`` and so
    never holds a branch.
    """
    return record.detached and record.path.name.startswith(REVIEW_WORKTREE_PREFIX)


def _deregister(repo: str, record: git.WorktreeRecord) -> bool:
    """Drop *record*'s registration, unlocking first when a bare lock blocks it."""
    if record.locked and not git.worktree_unlock(repo, str(record.path)):
        return False
    return git.worktree_remove(repo, str(record.path))


def _disposition(repo: str, record: git.WorktreeRecord, age: float | None, max_age: timedelta, *, dry_run: bool) -> str:
    """Dispose of one absent review registration, returning the line that reports it."""
    label = f"review registration {record.path}"
    if record.lock_reason:
        return f"KEPT {label}: locked with a reason ({record.lock_reason!r}) — a deliberate claim"
    if age is None:
        return f"KEPT {label}: its admin dir is unreadable here, so its age cannot be established"
    if age < max_age.total_seconds():
        return f"KEPT {label}: last touched {int(age)}s ago, inside the {int(max_age.total_seconds())}s floor"
    if dry_run:
        return f"WOULD Deregister {label} (directory gone)"
    if not _deregister(repo, record):
        return f"SKIPPED {label}: git refused to deregister it"
    return f"Deregistered {label} (directory gone)"


def reap_stale_review_worktrees(
    repo: str,
    *,
    max_age: timedelta = REVIEW_REGISTRATION_TTL,
    dry_run: bool = False,
) -> list[str]:
    """Deregister *repo*'s abandoned review checkouts; report every one it considered.

    A registration whose directory is still PRESENT says nothing and is skipped
    silently — that is an ordinary in-flight review. Everything else this touched or
    declined to touch gets a line, so a kept one is never a silent omission.
    """
    mtimes = _registration_mtimes(repo)
    now = time.time()
    reports: list[str] = []
    for record in git.list_worktrees(repo):
        if not is_review_checkout(record):
            continue
        match observe(record.path):
            case VenueObservation.PRESENT:
                continue
            case VenueObservation.UNOBSERVABLE:
                reports.append(
                    f"KEPT review registration {record.path}: unreachable from this process — not even "
                    f"its parent directory is readable here, which is missing evidence, not a deletion"
                )
                continue
            case VenueObservation.ABSENT:
                mtime = mtimes.get(record.path)
                age = None if mtime is None else now - mtime
                reports.append(_disposition(repo, record, age, max_age, dry_run=dry_run))
    if reports:
        logger.info("Review-worktree sweep in %s: %s", repo, "; ".join(reports))
    return reports


__all__ = ["REVIEW_REGISTRATION_TTL", "is_review_checkout", "reap_stale_review_worktrees"]
