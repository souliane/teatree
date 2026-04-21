"""Shared worktree cleanup logic used by sync (auto-clean on merge) and workspace commands.

The classifier below is the reason this module can be honest about squash-merges:
``git log --not --remotes`` detects commits by SHA, but a squash-merge creates a
new SHA on the default branch. Without subject-matching, every squash-merged
branch looks "unsynced" and blocks cleanup.
"""

import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from teatree.config import load_config
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.utils import git
from teatree.utils.db import drop_db

logger = logging.getLogger(__name__)

_PR_SUFFIX_RE = re.compile(r"(?:\s*\(#\d+\))+$")
_RELEASE_NOTE_SUFFIX_RE = re.compile(r"\s*\[[^\]]*\]\s*\([^)]+\)\s*$")
_TYPE_PREFIX_RE = re.compile(r"^[a-z]+(?:\([^)]+\))?!?:\s*", re.IGNORECASE)
_BRANCH_LOG_FIELDS = 3
_SUBJECT_PREVIEW_LIMIT = 3


@dataclass(frozen=True)
class BranchCommit:
    """A commit on a branch that is not reachable from any remote by SHA."""

    sha: str
    subject: str
    is_merge: bool


@dataclass(frozen=True)
class BranchClassification:
    """Structured view of a branch's unsynced commits, split by disposition.

    ``squash_merged`` — subject matches a commit on the target branch, so the
    content is already integrated (typical squash-merge case, including the
    ``relax:`` → ``feat:`` prefix rewrite).

    ``merge_commits`` — commits with multiple parents (Merge branch 'main' into
    feature). They carry no net content of their own and are safe to discard.

    ``genuinely_ahead`` — everything else. The branch has work that does not
    appear on the target, so removing it would lose content.
    """

    squash_merged: list[BranchCommit] = field(default_factory=list)
    merge_commits: list[BranchCommit] = field(default_factory=list)
    genuinely_ahead: list[BranchCommit] = field(default_factory=list)


def _canonicalize_subject(subject: str) -> str:
    """Normalize a commit subject for cross-branch matching.

    Strips, in order: trailing ``(#NNN)`` (added on squash-merge), trailing
    ``[flag] (ticket_url)`` (release-note suffix enforced by the MR-metadata
    hook — present on the merged title but usually absent from the local
    commit), and leading ``type(scope):`` so the ``relax:`` → ``feat(scope):``
    rewrite still matches.
    """
    stripped = _PR_SUFFIX_RE.sub("", subject).strip()
    stripped = _RELEASE_NOTE_SUFFIX_RE.sub("", stripped).strip()
    stripped = _TYPE_PREFIX_RE.sub("", stripped).strip()
    return stripped.lower()


def classify_branch_commits(repo: str, branch: str, target: str = "origin/main") -> BranchClassification:
    """Split the branch's unsynced commits into squash-merged / merge / genuinely-ahead buckets.

    Runs two git log invocations: one to list branch commits not on any remote
    (same as :func:`git.unsynced_commits`), one to fetch subjects on ``target``
    for subject matching.
    """
    raw = git.run(
        repo=repo,
        args=["log", branch, "--not", "--remotes", "--format=%H%x00%P%x00%s"],
    )
    classification = BranchClassification()
    if not raw.strip():
        return classification

    target_raw = git.run(repo=repo, args=["log", target, "--format=%s", "-n", "500"])
    target_subjects = {_canonicalize_subject(line) for line in target_raw.splitlines() if line.strip()}
    target_subjects.discard("")

    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x00", 2)
        if len(parts) < _BRANCH_LOG_FIELDS:
            continue
        sha, parents, subject = parts
        is_merge = len(parents.split()) > 1
        commit = BranchCommit(sha=sha, subject=subject, is_merge=is_merge)
        if is_merge:
            classification.merge_commits.append(commit)
        elif _canonicalize_subject(subject) in target_subjects:
            classification.squash_merged.append(commit)
        else:
            classification.genuinely_ahead.append(commit)
    return classification


def _raise_if_genuinely_ahead(repo_main: str, worktree: Worktree) -> None:
    """Raise ``RuntimeError`` when the branch carries commits not on ``origin/main``.

    Merge commits and squash-merged commits are ignored — only ``genuinely_ahead``
    work blocks cleanup. The error message lists up to ``_SUBJECT_PREVIEW_LIMIT``
    commit subjects so the caller can decide whether to push or abandon.
    """
    unsynced = git.unsynced_commits(repo_main, worktree.branch)
    if not unsynced:
        return
    classification = classify_branch_commits(repo_main, worktree.branch)
    if not classification.genuinely_ahead:
        return
    preview = classification.genuinely_ahead[:_SUBJECT_PREVIEW_LIMIT]
    subjects = ", ".join(c.subject for c in preview)
    if len(classification.genuinely_ahead) > _SUBJECT_PREVIEW_LIMIT:
        subjects += ", …"
    msg = (
        f"{worktree.repo_path} ({worktree.branch}): "
        f"refused cleanup — {len(classification.genuinely_ahead)} unsynced commit(s) "
        f"not on origin/main: {subjects}. "
        "Push them to a new branch or pass force=True."
    )
    raise RuntimeError(msg)


def cleanup_worktree(worktree: Worktree, *, force: bool = False) -> str:
    """Remove a single worktree: git worktree, branch, DB, overlay cleanup.

    Deletes the Worktree record from the database and returns a summary label.
    Errors in individual cleanup steps are suppressed so that partial cleanup
    still succeeds.

    Raises ``RuntimeError`` when *force* is ``False`` and the branch has local
    commits that are genuinely ahead of the default branch (i.e. not merge
    commits and not squash-merged under a new SHA). Pass ``force=True`` only
    from trusted callers (e.g. tests, programmatic API).
    """
    workspace = load_config().user.workspace_dir
    wt_path = (worktree.extra or {}).get("worktree_path", "")
    overlay = get_overlay()

    if wt_path and Path(wt_path).is_dir() and git.status_porcelain(wt_path):
        logger.warning("%s has uncommitted changes — cleaning anyway (PR merged)", worktree.repo_path)

    for step in overlay.get_cleanup_steps(worktree):
        with suppress(Exception):
            step.callable()

    if wt_path:
        repo_main = workspace / worktree.repo_path
        if repo_main.is_dir():
            if not force:
                _raise_if_genuinely_ahead(str(repo_main), worktree)
            git.worktree_remove(str(repo_main), wt_path)
            git.branch_delete(str(repo_main), worktree.branch)

    if worktree.db_name:
        drop_db(worktree.db_name)

    label = f"Cleaned: {worktree.repo_path} ({worktree.branch})"
    ticket_id = worktree.ticket.pk
    worktree.delete()
    if not Worktree.objects.filter(ticket_id=ticket_id).exists():
        Ticket.objects.get(pk=ticket_id).release_redis_slot()
    return label
