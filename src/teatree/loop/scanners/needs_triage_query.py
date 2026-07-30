"""Shared ``needs-triage`` open-issue query — the SSOT two scanners consume.

The ``needs-triage`` discovery half (issue field extractors + the assignee
fan-out that lists a host's OPEN ``needs-triage`` issues) is used by BOTH the
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


def needs_triage_issues(host: "CodeHostBackend", assignees: tuple[str, ...]) -> list[RawAPIDict]:
    """Every OPEN ``needs-triage`` issue assigned to one of *assignees*, deduped by URL.

    The single fan-out both the disposition and the assessor scanner consume: it
    lists each assignee's issues on *host*, keeps only the OPEN ones carrying
    :data:`~teatree.core.models.NEEDS_TRIAGE_LABEL`, and dedupes by issue URL so an
    issue assigned to two of the operator's aliases is returned once.
    """
    seen_urls: set[str] = set()
    issues: list[RawAPIDict] = []
    for assignee in assignees:
        try:
            fetched = host.list_assigned_issues(assignee=assignee)
        except Exception:
            logger.warning("list_assigned_issues failed for %s — skipping", assignee, exc_info=True)
            continue
        for issue in fetched:
            if not _issue_is_open(issue):
                continue
            if NEEDS_TRIAGE_LABEL not in _issue_labels(issue):
                continue
            url = _issue_url(issue)
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            issues.append(issue)
    return issues
