"""The ``t3 <overlay> workspace landscape`` intake survey (#2541).

Split from :mod:`workspace` to keep the command module under the per-module LOC
cap. Composes the deterministic gather in :mod:`teatree.core.landscape` from the
live workspace: the local worktree paths, the active overlay's code-host backend,
and the open issues for the repos in scope. Renders a JSON-serialisable survey
the ``/t3:ticket`` intake step surfaces and the planner consumes — open PRs,
in-flight worktrees, and a per-issue close/merge/supersede recommendation — so
the planner plans against the real landscape instead of re-deriving it.
"""

import logging
from pathlib import Path
from typing import TypedDict

from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.landscape import LandscapeSurvey, survey_landscape, survey_merged_pr_issue_numbers
from teatree.core.overlay_loader import get_overlay
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)


class WorktreeReport(TypedDict):
    """One in-flight local checkout, flattened for the JSON command output."""

    path: str
    branch: str
    has_uncommitted: bool
    has_unpushed: bool
    in_flight: bool


class OpenPrReport(TypedDict):
    """One open PR/MR, flattened for the JSON command output."""

    url: str
    title: str
    referenced_issues: list[int]


class RecommendationReport(TypedDict):
    """One per-issue verdict, flattened for the JSON command output."""

    issue_url: str
    title: str
    disposition: str
    action: str
    evidence: str


class LandscapeReport(TypedDict):
    """JSON-serialisable shape the ``landscape`` command returns.

    Mirrors :class:`~teatree.core.landscape.LandscapeSurvey` flattened for the
    CLI: ``worktrees`` are the in-flight local checkouts, ``open_prs`` the
    operator's open PRs/MRs, ``recommendations`` the per-issue verdicts, and
    ``warnings`` any probe that could not complete.
    """

    worktrees: list[WorktreeReport]
    open_prs: list[OpenPrReport]
    recommendations: list[RecommendationReport]
    warnings: list[str]


def _worktrees_of_clone(clone: Path) -> list[Path]:
    """Worktree paths registered against one main clone (``git worktree list``).

    A worktree's registry lives in its source clone, so the clone is the unit the
    porcelain listing runs against. An inconclusive listing (git error) yields no
    paths for that clone rather than raising, so one bad clone never aborts the
    sweep.
    """
    try:
        raw = git.run(repo=str(clone), args=["worktree", "list", "--porcelain"])
    except CommandFailedError:
        return []
    paths: list[Path] = []
    for line in raw.splitlines():
        if line.startswith("worktree "):
            candidate = Path(line.removeprefix("worktree ").strip())
            if candidate.is_dir():
                paths.append(candidate)
    return paths


def _workspace_worktree_paths(workspace: Path) -> list[Path]:
    """Enumerate every git worktree directory under the workspace.

    The workspace holds one or more main clones, each owning a worktree registry.
    Unions the worktree listing of every immediate-subdirectory clone (a dir with
    a ``.git``) plus the workspace root itself when it is a clone. A workspace
    that is not a directory, or holds no clones, yields an empty list rather than
    raising — the survey degrades to "no local landscape", never aborts intake.
    Paths are de-duplicated, preserving first-seen order.
    """
    if not workspace.is_dir():
        return []
    clones: list[Path] = []
    if (workspace / ".git").exists():
        clones.append(workspace)
    clones.extend(child for child in sorted(workspace.iterdir()) if child.is_dir() and (child / ".git").exists())
    seen: set[str] = set()
    paths: list[Path] = []
    for clone in clones:
        for wt in _worktrees_of_clone(clone):
            key = str(wt.resolve())
            if key not in seen:
                seen.add(key)
                paths.append(wt)
    return paths


def _open_issues_in_scope(host: CodeHostBackend, repo_slugs: list[str]) -> tuple[list[RawAPIDict], list[str]]:
    """List open issues for each repo slug in scope through the code host.

    Returns ``(issues, warnings)``. A repo whose issue listing fails degrades to
    a warning, never aborting the survey — a partial issue list is honest, a
    crashed intake is not. Uses the operator's assigned issues as the in-scope
    set (the canonical "issues I might act on" surface the scanners use).
    """
    issues: list[RawAPIDict] = []
    warnings: list[str] = []
    try:
        issues = list(host.list_assigned_issues(assignee=host.current_user()))
    except Exception as exc:  # noqa: BLE001 — any forge error degrades to a warning, never aborts intake
        warnings.append(f"could not list open issues in scope ({', '.join(repo_slugs) or 'no repos'}): {exc}")
    return issues, warnings


def _to_report(survey: LandscapeSurvey) -> LandscapeReport:
    """Flatten a :class:`LandscapeSurvey` into the JSON-serialisable command shape."""
    return LandscapeReport(
        worktrees=[
            WorktreeReport(
                path=str(wt.path),
                branch=wt.branch,
                has_uncommitted=wt.has_uncommitted,
                has_unpushed=wt.has_unpushed,
                in_flight=wt.in_flight,
            )
            for wt in survey.worktrees
        ],
        open_prs=[
            OpenPrReport(url=pr.url, title=pr.title, referenced_issues=sorted(pr.referenced_issues))
            for pr in survey.open_prs
        ],
        recommendations=[
            RecommendationReport(
                issue_url=rec.issue_url,
                title=rec.title,
                disposition=rec.disposition.value,
                action=rec.action.value,
                evidence=rec.evidence,
            )
            for rec in survey.recommendations
        ],
        warnings=list(survey.warnings),
    )


def run_landscape(workspace: Path) -> LandscapeReport:
    """Gather and render the intake landscape survey for the active overlay.

    Resolves the overlay code host (a missing host degrades to a warning-only
    report rather than failing — intake still benefits from the local git
    landscape), enumerates the workspace worktrees and in-scope open issues, then
    delegates classification to :func:`teatree.core.landscape.survey_landscape`.
    """
    worktree_paths = _workspace_worktree_paths(workspace)
    host = code_host_from_overlay()
    if host is None:
        local = survey_landscape(
            host=_NullCodeHost(),
            author="",
            worktree_paths=worktree_paths,
            open_issues=[],
        )
        report = _to_report(local)
        report["warnings"].append("no code host configured; surveyed local git landscape only")
        return report

    repo_slugs = get_overlay().get_merge_candidate_repo_slugs()
    open_issues, issue_warnings = _open_issues_in_scope(host, repo_slugs)
    try:
        author = host.current_user()
    except Exception as exc:  # noqa: BLE001 — degrade to empty author + warning, never abort
        author = ""
        issue_warnings.append(f"could not resolve current user: {exc}")

    merged_issue_numbers, merged_warnings = survey_merged_pr_issue_numbers(host, author=author)
    survey = survey_landscape(
        host=host,
        author=author,
        worktree_paths=worktree_paths,
        open_issues=open_issues,
        merged_pr_issue_numbers=merged_issue_numbers,
    )
    report = _to_report(survey)
    report["warnings"].extend(issue_warnings)
    report["warnings"].extend(merged_warnings)
    return report


class _NullCodeHost:
    """No-op code host so the local-only survey path reuses one gather routine."""

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return []

    def list_my_merged_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return []
