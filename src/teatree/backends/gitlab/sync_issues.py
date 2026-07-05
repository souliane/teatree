"""Issue sync functions extracted from ``gitlab_sync.py``.

Handles fetching assigned issues, resolving issues from URLs,
fetching issue labels, and extracting variant/process labels.
"""

import logging
import re
from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx

from teatree.backends.gitlab import GitLabCodeHost
from teatree.core.models import Ticket
from teatree.core.ticket_kind_classification import classify_ticket_kind
from teatree.types import SyncResult

if TYPE_CHECKING:
    from teatree.backends.gitlab.api import GitLabAPI
    from teatree.core.models.types import TicketExtra, TicketSiblingFields

logger = logging.getLogger(__name__)

_ISSUE_PARTS_RE = re.compile(r"https?://[^/]+/(.+?)/-/(?:issues|work_items)/(\d+)")


def fetch_assigned_issues(
    host: GitLabCodeHost,
    username: str,
    result: SyncResult,
    *,
    overlay_name: str = "",
) -> None:
    """Upsert tickets for issues assigned to *username* that have no PR yet.

    Tickets keyed by the same ``issue_url`` are consolidated with PR-based
    tickets so each ticket is represented by a single row.
    """
    try:
        issues = host.list_assigned_issues(assignee=username)
    except httpx.HTTPError as exc:
        result.errors.append(f"Assigned issues fetch failed: {exc}")
        return

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        issue_url = str(issue.get("web_url", ""))
        if not issue_url:
            continue
        result.issues_found += 1
        repo_path = extract_issue_repo_path(issue_url)
        repo_short = repo_path.rsplit("/", maxsplit=1)[-1] if repo_path else ""

        existing = Ticket.objects.filter(issue_url=issue_url).first()
        if existing is not None:
            if repo_short and isinstance(existing.repos, list) and repo_short not in existing.repos:
                existing.repos = [*existing.repos, repo_short]
                existing.save(update_fields=["repos"])
                result.tickets_updated += 1
            continue

        raw_labels = issue.get("labels")
        labels = [str(label) for label in raw_labels] if isinstance(raw_labels, list) else []
        issue_title = str(issue.get("title", ""))
        Ticket.objects.create(
            issue_url=issue_url,
            repos=[repo_short] if repo_short else [],
            extra={"issue_title": issue_title},
            state=Ticket.State.NOT_STARTED,
            overlay=overlay_name,
            kind=classify_ticket_kind(labels=labels, title=issue_title),
        )
        result.tickets_created += 1


def extract_issue_repo_path(issue_url: str) -> str:
    match = _ISSUE_PARTS_RE.search(issue_url)
    return match.group(1) if match else ""


def resolve_issue(
    client: "GitLabAPI",
    issue_url: str,
    *,
    ticket: Ticket | None = None,
) -> tuple[dict, str, int] | None:
    match = _ISSUE_PARTS_RE.search(issue_url)
    if not match:
        return None
    project_path, iid_str = match.group(1), match.group(2)
    iid = int(iid_str)
    if iid == 0:
        return None
    project = client.resolve_project(project_path)
    if not project:
        return None
    try:
        issue = client.get_issue(project.project_id, iid)
    except httpx.HTTPStatusError as exc:
        if ticket is not None and getattr(exc.response, "status_code", None) == HTTPStatus.NOT_FOUND:
            mark_tracker_404(ticket, project_path, iid)
        else:
            logger.warning("Failed to fetch issue %s#%d: %s", project_path, iid, exc)
        return None
    return (issue, project_path, iid) if issue else None


def mark_tracker_404(ticket: Ticket, project_path: str, iid: int) -> None:
    if (ticket.extra or {}).get("tracker_404"):
        return
    # #800 N3: canonical locked RMW (was an unlocked extra save).
    ticket.merge_extra(set_keys={"tracker_404": True})
    logger.info("Issue %s#%d returned 404; marked tracker_404 to skip future sync", project_path, iid)


def apply_issue_data(client: "GitLabAPI", ticket: Ticket, issue: dict, project_path: str, iid: int) -> bool:
    labels = issue.get("labels", [])
    tracker_status = process_label(labels) if isinstance(labels, list) else None

    if not tracker_status and "/work_items/" in ticket.issue_url:
        tracker_status = client.get_work_item_status(project_path, iid) or tracker_status

    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    set_keys: TicketExtra = {}

    if tracker_status and extra.get("tracker_status") != tracker_status:
        set_keys["tracker_status"] = tracker_status

    issue_title = str(issue.get("title", ""))
    if issue_title and extra.get("issue_title") != issue_title:
        set_keys["issue_title"] = issue_title

    also_set: TicketSiblingFields = {}
    variant = extract_variant(list(labels)) if isinstance(labels, list) else ""
    if variant and ticket.variant != variant:
        also_set["variant"] = variant

    if set_keys or also_set:
        # #800 N3: canonical locked RMW; changed extra keys + variant
        # one atomic write via also_set (no split, no unlocked clobber).
        ticket.merge_extra(set_keys=set_keys or None, also_set=also_set or None)
        return True
    return False


def fetch_issue_labels(client: "GitLabAPI", result: SyncResult) -> None:
    for ticket in Ticket.objects.exclude(issue_url="").filter(
        issue_url__regex=r"/-/(issues|work_items)/\d+$",
    ):
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        if extra.get("tracker_404"):
            continue
        resolved = resolve_issue(client, ticket.issue_url, ticket=ticket)
        if not resolved:
            continue
        issue, project_path, iid = resolved
        if apply_issue_data(client, ticket, issue, project_path, iid):
            result.labels_fetched += 1


def extract_variant(labels: list[object]) -> str:
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    known = get_overlay().config.known_variants
    known_lower = {v.lower(): v for v in known}
    for label in labels:
        text = str(label)
        match = known_lower.get(text.lower())
        if match:
            return match
    return ""


def process_label(labels: list[object]) -> str | None:
    for label in labels:
        text = str(label)
        if text.startswith(("Process::", "Process:: ")):
            return text
    return None
