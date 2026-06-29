"""Detect and guard against orphan branches.

An ORPHAN is a local branch that carries work not on the repo's default branch
(``origin/main``, ``origin/master``, or whatever ``refs/remotes/origin/HEAD``
points at — resolved per-repo) after subject-match and tree-equality checks
AND has no open PR on the remote. Orphans silently leak work: they accumulate
between weekly cleanups and are easy to miss when closing a session.

This module is the single source of truth used by the three enforcement
points that keep the no-orphan invariant:

- pre-push CLI (``t3 teatree pr ensure-pr``) — auto-create a PR before pushing an
    orphan so the branch has a tracking artifact from the first push.
- session-end hook — surface orphans in ``additionalContext`` so the agent
    sees them before the session closes.
- ``workspace ticket`` — warn before creating a new worktree when the
    workspace already contains orphans.
"""

from dataclasses import dataclass
from enum import StrEnum

from teatree.config import clone_root
from teatree.core.cleanup import _branch_tree_matches_squash, classify_branch_commits, probe_host_cli
from teatree.core.clone_paths import resolve_clone_path
from teatree.core.models import Worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError


class BranchStatus(StrEnum):
    """Classification of a branch's sync state against the repo's default branch."""

    SYNCED = "synced"
    OPEN_PR = "open_pr"
    UNPUSHED_ORPHAN = "unpushed_orphan"
    PUSHED_ORPHAN = "pushed_orphan"


_ORPHAN_STATUSES = frozenset({BranchStatus.UNPUSHED_ORPHAN, BranchStatus.PUSHED_ORPHAN})


@dataclass(frozen=True)
class BranchReport:
    """Sync status of a single branch in a single repo."""

    repo: str
    branch: str
    status: BranchStatus
    ahead_count: int
    open_pr_url: str = ""

    @property
    def is_orphan(self) -> bool:
        return self.status in _ORPHAN_STATUSES


def find_open_pr(repo: str, branch: str) -> str:
    """Return the URL of the open PR for ``branch``, or ``""`` if none.

    Queries GitHub (``gh pr list``) and GitLab (``glab mr list``). Returns ``""``
    when neither CLI is available (sandbox, CI without auth) — callers treat
    that as "no open PR known" rather than erroring.
    """
    url = probe_host_cli(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--limit", "1"],
        repo,
        lambda data: data[0]["url"],
    )
    if url:
        return url
    return probe_host_cli(
        ["glab", "mr", "list", "--source-branch", branch, "--state", "opened", "--output", "json", "-P", "1"],
        repo,
        lambda data: data[0]["web_url"],
    )


def _origin_default_branch_target(repo: str) -> str:
    """Resolve ``origin/<default-branch>`` for the repo, defaulting to ``origin/main``.

    A repo whose default branch is ``master`` (or any non-``main`` name) was
    misclassified as SYNCED because :func:`classify_branch_commits` defaulted
    to ``origin/main``. Resolving the actual default via ``git symbolic-ref
    refs/remotes/origin/HEAD`` makes the comparison authoritative.
    """
    try:
        return f"origin/{git.default_branch(repo=repo)}"
    except (CommandFailedError, RuntimeError, ValueError):
        return "origin/main"


def classify_branch(repo: str, branch: str) -> BranchReport:
    """Classify ``branch`` in ``repo`` as synced, open PR, or orphan (unpushed / pushed)."""
    target = _origin_default_branch_target(repo)
    classification = classify_branch_commits(repo, branch, target=target)
    ahead = len(classification.genuinely_ahead)

    if ahead == 0:
        return BranchReport(repo=repo, branch=branch, status=BranchStatus.SYNCED, ahead_count=0)

    if _branch_tree_matches_squash(repo, branch):
        return BranchReport(repo=repo, branch=branch, status=BranchStatus.SYNCED, ahead_count=ahead)

    pr_url = find_open_pr(repo, branch)
    if pr_url:
        return BranchReport(
            repo=repo,
            branch=branch,
            status=BranchStatus.OPEN_PR,
            ahead_count=ahead,
            open_pr_url=pr_url,
        )

    has_remote = bool(git.run(repo=repo, args=["ls-remote", "--heads", "origin", branch]))
    status = BranchStatus.PUSHED_ORPHAN if has_remote else BranchStatus.UNPUSHED_ORPHAN
    return BranchReport(repo=repo, branch=branch, status=status, ahead_count=ahead)


def find_orphans_in_workspace() -> list[BranchReport]:
    """Return orphan branches across all tracked worktrees in the workspace.

    Deduplicates by ``(repo, branch)`` — multiple Worktree rows sharing a
    branch produce a single report.
    """
    workspace = clone_root()
    reports: list[BranchReport] = []
    seen: set[tuple[str, str]] = set()
    for wt in Worktree.objects.all():
        repo_main = resolve_clone_path(workspace, wt)
        if repo_main is None or not repo_main.is_dir():
            continue
        key = (str(repo_main), wt.branch)
        if key in seen:
            continue
        seen.add(key)
        report = classify_branch(str(repo_main), wt.branch)
        if report.is_orphan:
            reports.append(report)
    return reports
