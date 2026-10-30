"""Shared ``needs-triage`` open-issue query — the SSOT two scanners consume.

The ``needs-triage`` discovery half (issue field extractors plus label and
assignee queries for a host's OPEN ``needs-triage`` issues) is used by BOTH the
:class:`~teatree.loop.scanners.issue_disposition.IssueDispositionScanner` (which
CLOSES high-confidence dead noise) and the
:class:`~teatree.loop.scanners.triage_assessor.TriageAssessorScanner` (which
QUEUES an assessment per issue behind an ask-gate). Carving it out keeps the two
scanners reading ONE definition of "an open needs-triage issue" — a change to the
label filter or the open-state test lands in both, never in one and not the other.

The extraction is byte-identical to the disposition scanner's original private
helpers (``tests/teatree_loop/scanners/test_issue_disposition.py`` pins the
behaviour), so ``issue_disposition`` re-imports these names rather than keeping
its own copies.
"""

import logging
from typing import TYPE_CHECKING, cast

from teatree.core.models import NEEDS_TRIAGE_LABEL
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

logger = logging.getLogger(__name__)


def _issue_url(issue: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = issue.get(name)
        if isinstance(value, str):
            return value
    return ""


def _issue_title(issue: RawAPIDict) -> str:
    title = issue.get("title")
    return title if isinstance(title, str) else ""


def _issue_body(issue: RawAPIDict) -> str:
    for name in ("body", "description"):
        value = issue.get(name)
        if isinstance(value, str):
            return value
    return ""


def _issue_labels(issue: RawAPIDict) -> list[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    out: list[str] = []
    for item in labels:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            name = cast("RawAPIDict", item).get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def _issue_is_open(issue: RawAPIDict) -> bool:
    state = issue.get("state")
    return not (isinstance(state, str) and state.lower() == "closed")


def _eligible_issue(issue: RawAPIDict) -> bool:
    return _issue_is_open(issue) and NEEDS_TRIAGE_LABEL in _issue_labels(issue)


def _labeled_issues(
    host: "CodeHostBackend", repo_slugs: tuple[str, ...], read_errors: list[str] | None
) -> list[RawAPIDict] | None:
    try:
        return host.list_labeled_issues(label=NEEDS_TRIAGE_LABEL, repo_slugs=repo_slugs)
    except Exception:
        if read_errors is None:
            raise
        logger.warning("list_labeled_issues failed — falling back to assigned issues", exc_info=True)
        read_errors.append("list_labeled_issues failed")
        return None


def _assigned_issues(
    host: "CodeHostBackend", assignees: tuple[str, ...], repo_slugs: tuple[str, ...], read_errors: list[str] | None
) -> list[list[RawAPIDict]]:
    sources: list[list[RawAPIDict]] = []
    for assignee in assignees:
        try:
            fetched = (
                host.list_assigned_issues(assignee=assignee, repo_slugs=repo_slugs)
                if repo_slugs
                else host.list_assigned_issues(assignee=assignee)
            )
        except Exception:
            logger.warning("list_assigned_issues failed for %s — skipping", assignee, exc_info=True)
            if read_errors is not None:
                read_errors.append(f"list_assigned_issues failed for {assignee}")
            continue
        sources.append(fetched)
    return sources


def needs_triage_issues(
    host: "CodeHostBackend",
    assignees: tuple[str, ...],
    *,
    repo_slugs: tuple[str, ...] = (),
    by_label: bool = False,
    read_errors: list[str] | None = None,
) -> list[RawAPIDict]:
    """Open ``needs-triage`` issues by label for owned repos, or by assignee.

    The single fan-out both the disposition and the assessor scanner consume. The
    assessor reads every labeled issue in its owned repositories. The
    disposition scanner reads only assigned issues. Both paths keep only open
    issues carrying :data:`~teatree.core.models.NEEDS_TRIAGE_LABEL` and dedupe by URL.
    """
    seen_urls: set[str] = set()
    issues: list[RawAPIDict] = []
    if by_label and repo_slugs:
        labeled = _labeled_issues(host, repo_slugs, read_errors)
        sources = [labeled] if labeled is not None else _assigned_issues(host, assignees, repo_slugs, read_errors)
    else:
        sources = _assigned_issues(host, assignees, repo_slugs, read_errors)
    for fetched in sources:
        for issue in fetched:
            if not _eligible_issue(issue):
                continue
            url = _issue_url(issue)
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            issues.append(issue)
    return issues
