"""Ticket-intake landscape survey — what is already in flight before planning (#2541).

The INTAKE state (the ``ticket`` phase, ``/t3:ticket``) is where teatree fetches
context BEFORE the planner designs a plan. Fetching the issue body alone is not
enough: a backlog accretes tickets whose work is already done, partially done,
deprecated, superseded, or won't-do, and local checkouts accrete unpushed
commits, stale worktrees, and open PRs that nobody remembers. The planner should
not re-derive that picture — it should be handed a *survey* and plan against it.

This module is the deterministic, gather-once-correctly half of that survey:

* **Local git landscape** — every worktree under the workspace, each tagged with
    its branch, whether it has uncommitted changes, and whether it carries commits
    absent from every remote (forgotten unpushed work). Django-free, depending only
    on :mod:`teatree.utils.git`, so it can run from any context (including a
    bare-``python3`` hook).
* **Forge landscape** — the operator's open PRs/MRs and the open issues for the
    repos in scope, gathered through an injected :class:`CodeHostBackend` (the
    Protocol, never a concrete backend — no integration-layer import).
* **Recommendations** — a coarse, deterministic classification of each open issue
    (``done`` / ``partial`` / ``superseded`` / ``open``) with a suggested action
    (``close`` / ``merge`` / ``supersede`` / ``keep``) so the planner gets a
    ready-to-consume list rather than re-running the same git+forge probes.

The classification is intentionally a heuristic floor, not a judge: it pins the
mechanical signals (a merged PR naming the issue ⇒ ``done``; an open PR naming the
issue ⇒ ``partial``/``merge``; an open worktree/branch for the issue ⇒ work in
flight). The planner agent — which reads the prose — refines the verdict; the
survey guarantees the planner never *misses* an in-flight artifact, which is the
failure this closes.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)


class PrLister(Protocol):
    """The one code-host capability the survey needs — list the operator's PRs.

    A consumer-defined narrow Protocol (a structural subset of
    :class:`~teatree.core.backend_protocols.CodeHostBackend`): the survey only
    ever lists PRs, so it depends on this single method rather than the full
    backend. The real backend satisfies it structurally, and the local-only
    null host needs to implement only this one method.
    """

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]: ...


# Match a ``#<number>`` issue reference in a PR title/body — the squash-merge
# ``(#N)`` convention as well as a ``Closes #N`` / ``Fixes #N`` form.
_ISSUE_REF = re.compile(r"#(\d+)")


class IssueDisposition(StrEnum):
    """How an open issue stands against the current code landscape.

    ``DONE`` — a *merged* PR names the issue; the work shipped but the issue is
    still open (the squash-merge ``(#N)`` case that leaves the issue OPEN).
    ``PARTIAL`` — an *open* PR names the issue; work is in flight, not yet merged.
    ``SUPERSEDED`` — another open issue/PR covers the same surface (heuristic;
    the planner confirms). ``OPEN`` — no in-flight signal found; genuine work.
    """

    DONE = "done"
    PARTIAL = "partial"
    SUPERSEDED = "superseded"
    OPEN = "open"


class RecommendedAction(StrEnum):
    """The action the survey recommends the planner/operator take on an issue.

    ``CLOSE`` — the work is shipped (DONE); close the issue citing the merged PR.
    ``MERGE`` — an open PR carries the work (PARTIAL); finish + merge that PR
    rather than starting fresh. ``SUPERSEDE`` — close as superseded by the named
    sibling. ``KEEP`` — genuine open work; plan it.
    """

    CLOSE = "close"
    MERGE = "merge"
    SUPERSEDE = "supersede"
    KEEP = "keep"


@dataclass(frozen=True)
class WorktreeState:
    """One local git worktree's in-flight state.

    ``path`` is the absolute worktree directory (its canonical identity).
    ``has_uncommitted`` flags a dirty working tree (an agent may be mid-task).
    ``has_unpushed`` flags commits absent from every remote — forgotten work
    that a fresh plan would duplicate. Both fail *open*: an inconclusive git
    probe is reported as ``True`` so the survey never silently hides in-flight
    work.
    """

    path: Path
    branch: str
    has_uncommitted: bool
    has_unpushed: bool

    @property
    def in_flight(self) -> bool:
        """Whether this worktree holds work a new plan must not duplicate."""
        return self.has_uncommitted or self.has_unpushed


@dataclass(frozen=True)
class OpenPullRequest:
    """One of the operator's open PRs/MRs, as the survey records it.

    ``url`` is the canonical identity (never a bare iid). ``referenced_issues``
    is the set of ``#N`` issue numbers named in the title/body, so the survey can
    tie a PR to the issues it advances.
    """

    url: str
    title: str
    referenced_issues: frozenset[int]


@dataclass(frozen=True)
class IssueRecommendation:
    """A survey verdict for one open issue, ready for the planner to consume.

    ``disposition`` is the mechanical classification; ``action`` the suggested
    next step; ``evidence`` cites the artifact (the PR URL / sibling issue) that
    drove the verdict, so the planner/operator can act without re-probing.
    """

    issue_url: str
    title: str
    disposition: IssueDisposition
    action: RecommendedAction
    evidence: str = ""


@dataclass(frozen=True)
class LandscapeSurvey:
    """The full intake landscape the planner consumes instead of re-deriving it.

    ``worktrees`` is the local git picture (dirty / unpushed checkouts).
    ``open_prs`` is the operator's open PRs/MRs. ``recommendations`` is the
    per-issue classification + suggested action. ``warnings`` carries any probe
    that could not complete (a forge that failed to list, a corrupt worktree),
    so a partial survey is honest about what it could not see rather than
    presenting silence as "nothing in flight".
    """

    worktrees: list[WorktreeState] = field(default_factory=list)
    open_prs: list[OpenPullRequest] = field(default_factory=list)
    recommendations: list[IssueRecommendation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def in_flight_worktrees(self) -> list[WorktreeState]:
        """Worktrees holding uncommitted or unpushed work."""
        return [wt for wt in self.worktrees if wt.in_flight]

    @property
    def actionable(self) -> list[IssueRecommendation]:
        """Issues whose recommended action is not a plain ``KEEP``."""
        return [r for r in self.recommendations if r.action is not RecommendedAction.KEEP]


def _branch_of(wt_path: Path) -> str:
    """Resolve the checked-out branch of a worktree, or ``HEAD`` when detached.

    Fails to the literal ``HEAD`` (``git.DETACHED_HEAD``) on any probe error so
    the caller still records the worktree rather than dropping it.
    """
    try:
        branch = git.current_branch(str(wt_path))
    except CommandFailedError:
        return git.DETACHED_HEAD
    return branch or git.DETACHED_HEAD


def _has_unpushed(wt_path: Path, branch: str) -> bool:
    """Whether ``branch`` carries commits absent from every remote (fails open)."""
    ref = git.DETACHED_HEAD if branch == git.DETACHED_HEAD else branch
    try:
        return bool(git.commits_absent_from_all_remotes(str(wt_path), ref))
    except CommandFailedError:
        return True


def survey_worktrees(worktree_paths: list[Path]) -> list[WorktreeState]:
    """Gather the in-flight state of each local worktree.

    For each path, record its branch, whether the working tree is dirty, and
    whether it carries unpushed commits. A path that is not a directory is
    skipped (a stale registry entry). Both flags fail *open* — an inconclusive
    probe reports ``True`` — so the survey never hides work that might be lost.
    """
    states: list[WorktreeState] = []
    for path in worktree_paths:
        if not path.is_dir():
            continue
        branch = _branch_of(path)
        try:
            dirty = bool(git.status_porcelain(str(path)))
        except CommandFailedError:
            dirty = True
        states.append(
            WorktreeState(
                path=path,
                branch=branch,
                has_uncommitted=dirty,
                has_unpushed=_has_unpushed(path, branch),
            )
        )
    return states


def _referenced_issues(raw: RawAPIDict) -> frozenset[int]:
    """Extract the ``#N`` issue numbers a PR's title + body name."""
    text = f"{raw.get('title', '')}\n{raw.get('body', '')}\n{raw.get('description', '')}"
    return frozenset(int(m) for m in _ISSUE_REF.findall(text))


def _pr_url(raw: RawAPIDict) -> str:
    """The PR's canonical URL across forge payload shapes (never a bare iid)."""
    for key in ("url", "web_url", "html_url"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def survey_open_prs(host: PrLister, *, author: str) -> tuple[list[OpenPullRequest], list[str]]:
    """Gather the operator's open PRs/MRs through the code-host Protocol.

    Returns ``(prs, warnings)``. A forge that fails to list is reported as a
    warning rather than crashing the survey — a partial landscape is better than
    none, and the warning keeps the gap honest.
    """
    warnings: list[str] = []
    try:
        raw_prs = host.list_my_prs(author=author)
    except Exception as exc:  # noqa: BLE001 — any forge error degrades to a warning, never aborts intake
        warnings.append(f"could not list open PRs for {author}: {exc}")
        return [], warnings
    prs = [
        OpenPullRequest(url=_pr_url(raw), title=str(raw.get("title", "")), referenced_issues=_referenced_issues(raw))
        for raw in raw_prs
    ]
    return prs, warnings


class MergedPrLister(Protocol):
    """The code-host capability for the resolved-but-open (§1b) signal.

    A second consumer-defined narrow Protocol (alongside :class:`PrLister`):
    listing the operator's *merged* PRs so the survey can mark an issue named by
    a merged PR ``DONE``/``close``. The real backend satisfies it structurally
    via :meth:`CodeHostBackend.list_my_merged_prs`.
    """

    def list_my_merged_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]: ...


def survey_merged_pr_issue_numbers(host: MergedPrLister, *, author: str) -> tuple[frozenset[int], list[str]]:
    """Gather the issue numbers named by the operator's *merged* PRs.

    Returns ``(numbers, warnings)`` — the set feeds
    :func:`classify_issue`'s DONE/close path (an open issue whose work already
    shipped). A forge that fails to list degrades to a warning rather than
    crashing intake, exactly like :func:`survey_open_prs`.
    """
    warnings: list[str] = []
    try:
        raw_prs = host.list_my_merged_prs(author=author)
    except Exception as exc:  # noqa: BLE001 — any forge error degrades to a warning, never aborts intake
        warnings.append(f"could not list merged PRs for {author}: {exc}")
        return frozenset(), warnings
    numbers: set[int] = set()
    for raw in raw_prs:
        numbers |= _referenced_issues(raw)
    return frozenset(numbers), warnings


def _issue_number(issue_url: str) -> int | None:
    """The trailing issue number in a forge issue URL, or ``None``."""
    match = re.search(r"/(\d+)/?$", issue_url)
    return int(match.group(1)) if match else None


def classify_issue(
    issue: RawAPIDict,
    *,
    open_prs: list[OpenPullRequest],
    merged_pr_issue_numbers: frozenset[int],
) -> IssueRecommendation:
    """Classify one open issue against the in-flight PR landscape.

    The mechanical floor: a *merged* PR naming the issue ⇒ DONE/close; an *open*
    PR naming it ⇒ PARTIAL/merge (finish that PR, don't start fresh); otherwise
    OPEN/keep. The planner refines; the survey guarantees the signal is surfaced.
    """
    issue_url = str(issue.get("url") or issue.get("web_url") or issue.get("html_url") or "")
    title = str(issue.get("title", ""))
    number = _issue_number(issue_url)

    if number is not None and number in merged_pr_issue_numbers:
        return IssueRecommendation(
            issue_url=issue_url,
            title=title,
            disposition=IssueDisposition.DONE,
            action=RecommendedAction.CLOSE,
            evidence=f"merged PR references #{number}",
        )

    if number is not None:
        for pr in open_prs:
            if number in pr.referenced_issues:
                return IssueRecommendation(
                    issue_url=issue_url,
                    title=title,
                    disposition=IssueDisposition.PARTIAL,
                    action=RecommendedAction.MERGE,
                    evidence=f"open PR {pr.url} references #{number}",
                )

    return IssueRecommendation(
        issue_url=issue_url,
        title=title,
        disposition=IssueDisposition.OPEN,
        action=RecommendedAction.KEEP,
    )


def survey_landscape(
    *,
    host: PrLister,
    author: str,
    worktree_paths: list[Path],
    open_issues: list[RawAPIDict],
    merged_pr_issue_numbers: frozenset[int] = frozenset(),
) -> LandscapeSurvey:
    """Assemble the full intake landscape for the planner to consume.

    Gathers the local worktree state, the operator's open PRs, and a per-issue
    recommendation, returning one :class:`LandscapeSurvey`. Forge probe failures
    degrade to ``warnings`` rather than aborting — intake must not be blocked by
    a transient forge outage, only made honest about what it could not see.

    ``merged_pr_issue_numbers`` is the set of issue numbers named by *merged*
    PRs (the §1b resolved-but-open signal, gathered by the caller from the forge
    search); passing it lets the survey mark already-shipped issues DONE/close.
    """
    worktrees = survey_worktrees(worktree_paths)
    open_prs, warnings = survey_open_prs(host, author=author)
    recommendations = [
        classify_issue(issue, open_prs=open_prs, merged_pr_issue_numbers=merged_pr_issue_numbers)
        for issue in open_issues
    ]
    return LandscapeSurvey(
        worktrees=worktrees,
        open_prs=open_prs,
        recommendations=recommendations,
        warnings=warnings,
    )
