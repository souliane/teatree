"""Shared worktree cleanup logic used by sync (auto-clean on merge) and workspace commands.

The classifier below is the reason this module can be honest about squash-merges:
``git log <branch> --not origin/main`` detects commits by SHA, but a squash-merge
creates a new SHA on the default branch. Without subject-matching, every
squash-merged branch looks "unsynced" and blocks cleanup. Comparing against
``origin/main`` (not ``--remotes``) is essential — ``--remotes`` would also
exclude the feature branch's own remote tracking ref, hiding commits that are
pushed but not yet on main.
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from teatree.config import load_config
from teatree.core.clone_paths import resolve_clone_path
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.utils import git
from teatree.utils.db import drop_db
from teatree.utils.run import run_allowed_to_fail

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
        args=["log", branch, "--not", target, "--format=%H%x00%P%x00%s"],
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


def _pr_merge_commit_sha(repo: str, branch: str) -> str:
    """Return the SHA of the merge/squash commit for ``branch``'s merged PR, or ``""``.

    Queries GitHub (``gh pr list``) and GitLab (``glab mr list``) for a merged
    PR/MR whose source branch matches. The merge commit's tree captures the
    branch's net content at merge time — used by :func:`_branch_tree_matches_squash`
    to distinguish post-merge follow-up commits already captured by the squash
    from commits that add new content.

    Returns ``""`` when neither CLI is available (sandbox, CI without auth) —
    the caller falls back to subject-match classification.
    """
    sha = probe_host_cli(
        ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "mergeCommit", "--limit", "1"],
        repo,
        lambda data: data[0]["mergeCommit"]["oid"],
    )
    if sha:
        return sha
    return probe_host_cli(
        ["glab", "mr", "list", "--merged", "--source-branch", branch, "--output", "json", "-P", "1"],
        repo,
        lambda data: data[0]["merge_commit_sha"],
    )


def probe_host_cli(cmd: list[str], repo: str, extract: Callable[[Any], str]) -> str:
    """Invoke a host CLI that may be missing, parse its JSON, extract the SHA.

    Swallows ``OSError`` (missing binary, permission denied in sandboxes) and
    JSON/key errors — both are legitimate "no merged PR found" outcomes.
    """
    try:
        result = run_allowed_to_fail(cmd, cwd=repo, expected_codes=None)
    except OSError:
        return ""
    if result.returncode != 0 or result.stdout.strip() in {"", "[]"}:
        return ""
    try:
        data = json.loads(result.stdout)
        sha = extract(data) if data else ""
    except (json.JSONDecodeError, IndexError, KeyError, TypeError):
        return ""
    return sha or ""


def _branch_tree_matches_squash(repo: str, branch: str) -> bool:
    """Return ``True`` when the PR's merge commit has the same tree as the branch tip.

    Post-merge follow-up commits (retro, docs) appear as ``genuinely_ahead``
    because their subjects don't match the squash commit's final message.
    When their cumulative effect is already captured in the squash tree, the
    branch is safe to clean despite the unmatched subjects.
    """
    merge_sha = _pr_merge_commit_sha(repo, branch)
    if not merge_sha:
        return False
    return git.check(repo=repo, args=["diff", "--quiet", merge_sha, branch])


def _raise_if_genuinely_ahead(repo_main: str, worktree: Worktree) -> None:
    """Raise ``RuntimeError`` when the branch carries commits not on ``origin/main``.

    Merge commits and squash-merged commits are ignored — only ``genuinely_ahead``
    work blocks cleanup. As a fallback, the PR's merge commit tree is compared
    against the branch tip: an empty diff means the cumulative branch content
    is already captured in the squash (typical for post-merge retro commits),
    so cleanup proceeds. The error message lists up to ``_SUBJECT_PREVIEW_LIMIT``
    commit subjects so the caller can decide whether to push or abandon.
    """
    unsynced = git.unsynced_commits(repo_main, worktree.branch)
    if not unsynced:
        return
    classification = classify_branch_commits(repo_main, worktree.branch)
    if not classification.genuinely_ahead:
        return
    if _branch_tree_matches_squash(repo_main, worktree.branch):
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


def _resolve_worktree_path(workspace: Path, worktree: Worktree) -> str:
    """Return the on-disk worktree path, preferring extras and falling back to the canonical layout.

    Provisioning records ``worktree_path`` in ``Worktree.extra`` after a
    successful ``git worktree add``. When that record is missing (extras lost,
    row created before the path was set, manual provisioning), derive the path
    from the canonical layout used by ``WorktreeProvisioner._create``:
    ``workspace/<branch>/<repo-leaf>``.
    """
    stored = (worktree.extra or {}).get("worktree_path", "")
    if stored:
        return stored
    return str(workspace / worktree.branch / Path(worktree.repo_path).name)


def _remove_git_worktree(repo_main: Path, wt_path: str, worktree: Worktree, *, force: bool) -> list[str]:
    """Remove the git worktree + branch from the source repo, returning any error messages.

    Returns an empty list on success. The source repo missing, the git worktree
    remove failing, or the branch delete failing each yield one entry. Failures
    are surfaced (not raised) so unrelated cleanup steps still run.
    """
    if not repo_main.is_dir():
        return [f"source repo missing at {repo_main}"]
    if not force:
        _raise_if_genuinely_ahead(str(repo_main), worktree)
    errors: list[str] = []
    if not git.worktree_remove(str(repo_main), wt_path):
        errors.append(f"git worktree remove failed for {wt_path}")
    if not git.branch_delete(str(repo_main), worktree.branch):
        errors.append(f"git branch -D failed for {worktree.branch}")
    return errors


def cleanup_worktree(worktree: Worktree, *, force: bool = False) -> str:
    """Remove a single worktree: git worktree, branch, DB, overlay cleanup.

    Deletes the Worktree record from the database and returns a summary label.
    Errors in individual cleanup steps are suppressed so that partial cleanup
    still succeeds, but they are surfaced in the returned label so the caller
    can detect partial failures.

    Raises ``RuntimeError`` when *force* is ``False`` and the branch has local
    commits that are genuinely ahead of the default branch (i.e. not merge
    commits and not squash-merged under a new SHA). Pass ``force=True`` only
    from trusted callers (e.g. tests, programmatic API).
    """
    workspace = load_config().user.workspace_dir
    wt_path = _resolve_worktree_path(workspace, worktree)
    overlay = get_overlay()

    if Path(wt_path).is_dir() and git.status_porcelain(wt_path):
        logger.warning("%s has uncommitted changes — cleaning anyway (PR merged)", worktree.repo_path)

    step_errors: list[str] = []
    for step in overlay.get_cleanup_steps(worktree):
        try:
            step.callable()
        except Exception as exc:
            logger.exception("cleanup step failed for %s: %s", worktree.repo_path, step.description)
            step_errors.append(f"{step.description}: {exc}")

    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    step_errors.extend(_remove_git_worktree(repo_main, wt_path, worktree, force=force))

    if worktree.db_name:
        drop_db(worktree.db_name)

    label = f"Cleaned: {worktree.repo_path} ({worktree.branch})"
    if step_errors:
        label += f" [with errors: {'; '.join(step_errors)}]"
    ticket_id = worktree.ticket.pk
    worktree.delete()
    if not Worktree.objects.filter(ticket_id=ticket_id).exists():
        Ticket.objects.get(pk=ticket_id).release_redis_slot()
    return label
