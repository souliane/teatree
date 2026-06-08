"""Classify a branch's unsynced commits and ask the forge whether its PR merged.

The teardown data-loss guards in :mod:`teatree.core.cleanup` need to tell three
things apart: commits already integrated by a squash-merge (safe), merge commits
(no net content, safe), and genuinely-ahead work (would be lost). The subject
matcher here is the reason cleanup can be honest about squash-merges:
``git log <branch> --not origin/main`` detects commits by SHA, but a squash-merge
creates a NEW SHA on the default branch. Without subject-matching, every
squash-merged branch looks "unsynced" and blocks cleanup. Comparing against
``origin/main`` (not ``--remotes``) is essential — ``--remotes`` would also
exclude the feature branch's own remote tracking ref, hiding commits that are
pushed but not yet on main.

For branches that diverged so far the subject matcher and the squash-tree
heuristic both break down, :func:`_branch_pr_is_merged` asks the forge directly
(``gh``/``glab``) — the canonical merged signal (#1578).
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from teatree.utils import git
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

_PR_SUFFIX_RE = re.compile(r"(?:\s*\(#\d+\))+$")
_RELEASE_NOTE_SUFFIX_RE = re.compile(r"\s*\[[^\]]*\]\s*\([^)]+\)\s*$")
_TYPE_PREFIX_RE = re.compile(r"^[a-z]+(?:\([^)]+\))?!?:\s*", re.IGNORECASE)
_BRANCH_LOG_FIELDS = 3


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
    ``[flag] (ticket_url)`` (release-note suffix enforced by the PR-metadata
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
    PR whose source branch matches. The merge commit's tree captures the
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


def probe_host_cli(cmd: list[str], repo: str, extract: Callable[[Any], str], *, timeout: float = 30.0) -> str:
    """Invoke a host CLI that may be missing, parse its JSON, extract the SHA.

    Swallows ``OSError`` (missing binary, permission denied in sandboxes) and
    JSON/key errors — both are legitimate "no merged PR found" outcomes.

    ``timeout`` bounds the host CLI invocation (seconds): a hung ``gh``/``glab``
    must not block ``clean-all`` or the loop tick. On expiry the
    ``subprocess.TimeoutExpired`` is swallowed and ``""`` is returned — the same
    fail-safe "not found / skip" value as every other failure path, so a timeout
    can never produce a positive merged signal and never wrongly reaps work.
    """
    try:
        result = run_allowed_to_fail(cmd, cwd=repo, expected_codes=None, timeout=timeout)
    except (OSError, TimeoutExpired):
        return ""
    if result.returncode != 0 or result.stdout.strip() in {"", "[]"}:
        return ""
    try:
        data = json.loads(result.stdout)
        sha = extract(data) if data else ""
    except (json.JSONDecodeError, IndexError, KeyError, TypeError):
        return ""
    return sha or ""


def _branch_pr_is_merged(repo: str, branch: str) -> bool:
    """Whether the forge canonically reports ``branch``'s PR/MR as merged (#1578).

    The subject-match classifier and :func:`_branch_tree_matches_squash` both
    break down for branches that diverged long before they were squash-merged:
    the squash creates a new SHA on the default branch (so no subject matches and
    the branch's own SHAs are absent from every remote) and the branch tip tree
    no longer equals the squash commit tree (main moved on). Such a worktree is
    fully merged yet looks ``genuinely_ahead`` / "commits on NO remote", so the
    guards refuse it forever.

    This asks the forge directly — the canonical truth, not a heuristic. A merged
    PR/MR whose source branch matches ``branch`` means the work shipped, however
    far the local branch has since diverged. GitHub marks a squash-merged PR
    ``state=merged``; GitLab marks the MR ``merged`` — both are covered by the
    same ``--state merged`` / ``--merged`` queries the squash-commit probe uses,
    so this reuses :func:`probe_host_cli` (which swallows a missing ``gh``/``glab``
    binary and any parse error as "not found").

    **Fail-safe to skip.** Returns ``True`` only on a positive merged signal;
    every uncertain outcome (no merged PR, CLI absent, probe/JSON failure) returns
    ``False`` so the caller keeps the conservative refuse-and-report — ambiguity
    never reaps real work.
    """
    found = probe_host_cli(
        ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number", "--limit", "1"],
        repo,
        lambda data: str(data[0]["number"]),
    )
    if found:
        return True
    found = probe_host_cli(
        ["glab", "mr", "list", "--merged", "--source-branch", branch, "--output", "json", "-P", "1"],
        repo,
        lambda data: str(data[0]["iid"]),
    )
    return bool(found)


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
