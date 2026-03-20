"""Sync external data (GitLab MRs, issues) into the local database.

Fetches the authenticated user's open MRs across all accessible projects
and upserts them as Tickets.
"""

import re
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from teetree.core.models import Ticket
from teetree.utils.gitlab_api import GitLabAPI, ProjectInfo

LAST_SYNC_CACHE_KEY = "teatree_followup_last_sync"

_REPO_PATH_RE = re.compile(r"gitlab\.com/(.+?)/-/merge_requests/")


@dataclass(slots=True)
class SyncResult:
    mrs_found: int = 0
    tickets_created: int = 0
    tickets_updated: int = 0
    labels_fetched: int = 0
    mrs_merged: int = 0
    reviews_synced: int = 0
    errors: list[str] = field(default_factory=list)


def sync_followup() -> SyncResult:
    """Fetch all open MRs for the current user and upsert Tickets.

    Uses the global ``/merge_requests`` endpoint so no per-repo configuration
    is needed.  On subsequent runs, only fetches MRs updated since the last
    successful sync (using GitLab's ``updated_after`` filter).
    """
    token = getattr(settings, "TEATREE_GITLAB_TOKEN", "")
    if not token:
        return SyncResult(errors=["TEATREE_GITLAB_TOKEN is not set"])

    client = GitLabAPI(token=token)
    username = getattr(settings, "TEATREE_GITLAB_USERNAME", "") or client.current_username()
    if not username:
        return SyncResult(errors=["TEATREE_GITLAB_USERNAME is not set"])
    result = SyncResult()

    last_sync: str | None = cache.get(LAST_SYNC_CACHE_KEY)
    sync_started_at = timezone.now()

    try:
        mrs = client.list_all_open_mrs(username, updated_after=last_sync)
    except Exception as exc:  # noqa: BLE001
        return SyncResult(errors=[f"MR fetch failed: {exc}"])

    for mr in mrs:
        result.mrs_found += 1
        repo_path = _extract_repo_path(mr)
        project_id = int(mr.get("project_id", 0))  # type: ignore[arg-type]
        project = (
            ProjectInfo(
                project_id=project_id,
                path_with_namespace=repo_path,
                short_name=repo_path.rsplit("/", maxsplit=1)[-1],
            )
            if project_id
            else None
        )
        _upsert_ticket_from_mr(mr, repo_path, client, project, result, username=username)

    _fetch_issue_labels(client, result)
    _detect_merged_mrs(client, username, result, last_sync)

    cache.set(LAST_SYNC_CACHE_KEY, sync_started_at.isoformat(), timeout=None)

    return result


def _extract_repo_path(mr: dict[str, object]) -> str:
    """Extract the GitLab project path from an MR's ``web_url``."""
    web_url = str(mr.get("web_url", ""))
    match = _REPO_PATH_RE.search(web_url)
    return match.group(1) if match else ""


def _upsert_ticket_from_mr(  # noqa: PLR0913, PLR0914
    mr: dict[str, object],
    repo_path: str,
    client: GitLabAPI,
    project: ProjectInfo | None,
    result: SyncResult,
    *,
    username: str = "",
) -> None:
    issue_url = _extract_issue_url(mr)
    web_url = str(mr.get("web_url", ""))
    title = str(mr.get("title", ""))
    source_branch = str(mr.get("source_branch", ""))
    is_draft = bool(mr.get("draft"))
    mr_iid = int(mr.get("iid", 0))  # type: ignore[arg-type]
    repo_short = repo_path.rsplit("/", maxsplit=1)[-1]

    mr_entry: dict[str, object] = {
        "url": web_url,
        "title": title,
        "branch": source_branch,
        "draft": is_draft,
        "repo": repo_short,
        "iid": mr_iid,
    }

    # Enrich non-draft MRs with pipeline and approval data
    if not is_draft and project and mr_iid:
        pipeline = client.get_mr_pipeline(project.project_id, mr_iid)
        mr_entry["pipeline_status"] = pipeline["status"]
        mr_entry["pipeline_url"] = pipeline["url"]

        approvals = client.get_mr_approvals(project.project_id, mr_iid)
        mr_entry["approvals"] = approvals

        discussions = client.get_mr_discussions(project.project_id, mr_iid)
        mr_entry["discussions"] = _classify_discussions(discussions, username)

    # Reviewer info is available on all MRs (including drafts)
    reviewers = mr.get("reviewers", [])
    if isinstance(reviewers, list):
        mr_entry["review_requested"] = bool(reviewers)
        mr_entry["reviewer_names"] = [str(r.get("username", "")) for r in reviewers if isinstance(r, dict)]  # ty: ignore[no-matching-overload]

    lookup_url = issue_url or web_url
    inferred_state = _infer_state_from_mrs({web_url: mr_entry})
    ticket, created = Ticket.objects.get_or_create(
        issue_url=lookup_url,
        defaults={"repos": [repo_short], "extra": {"mrs": {web_url: mr_entry}}, "state": inferred_state},
    )
    if created:
        result.tickets_created += 1
    else:
        _update_ticket(ticket, mr_entry, web_url, repo_short, inferred_state)
        result.tickets_updated += 1


def _classify_discussions(
    discussions: list[dict[str, object]],
    author_username: str,
) -> list[dict[str, str]]:
    """Classify MR discussion threads into review comment statuses.

    Returns a list of dicts with keys: status, detail.
    Statuses: "waiting_reviewer", "needs_reply", "addressed".
    """
    result: list[dict[str, str]] = []
    for disc in discussions:
        if not isinstance(disc, dict):
            continue
        if disc.get("individual_note"):
            continue
        notes = disc.get("notes", [])
        if not isinstance(notes, list) or not notes:
            continue

        first_body = str(notes[0].get("body", "")) if isinstance(notes[0], dict) else ""  # ty: ignore[no-matching-overload]
        resolvable_notes = [n for n in notes if isinstance(n, dict) and n.get("resolvable")]  # ty: ignore[invalid-argument-type]
        all_resolved = bool(resolvable_notes) and all(n.get("resolved") for n in resolvable_notes)  # ty: ignore[invalid-argument-type]

        if all_resolved:
            status = "addressed"
        else:
            last_note = notes[-1]
            last_author = str(last_note.get("author", {}).get("username", "")) if isinstance(last_note, dict) else ""  # ty: ignore[no-matching-overload]
            status = "waiting_reviewer" if last_author == author_username else "needs_reply"

        result.append({"status": status, "detail": first_body[:120]})
    return result


_SKILL_WRITTEN_FIELDS = ("review_channel", "review_permalink", "e2e_test_plan_url", "notion_status", "notion_url")


def _update_ticket(
    ticket: Ticket,
    mr_entry: dict[str, object],
    mr_url: str,
    repo_short: str,
    inferred_state: str = "",
) -> None:
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    mrs = extra.get("mrs", {})
    if not isinstance(mrs, dict):
        mrs = {}

    # Preserve fields written by skills (Slack data, E2E links) that sync can't fetch
    prev = mrs.get(mr_url)
    if isinstance(prev, dict):
        for key in _SKILL_WRITTEN_FIELDS:
            if key in prev and key not in mr_entry:
                mr_entry[key] = prev[key]

    mrs[mr_url] = mr_entry
    extra["mrs"] = mrs

    repos = ticket.repos if isinstance(ticket.repos, list) else []
    if repo_short not in repos:
        repos = [*repos, repo_short]

    update_fields = ["extra", "repos"]

    # Advance state forward only — never regress
    if inferred_state and _STATE_ORDER.index(inferred_state) > _STATE_ORDER.index(ticket.state):
        ticket.state = inferred_state
        update_fields.append("state")

    ticket.extra = extra
    ticket.repos = repos
    ticket.save(update_fields=update_fields)


def _detect_merged_mrs(
    client: GitLabAPI,
    username: str,
    result: SyncResult,
    last_sync: str | None,
) -> None:
    """Detect MRs merged since last sync and clean up their ticket data.

    Fetches recently merged MRs, cross-references with in-flight tickets,
    removes stale discussion data, and advances ticket state to MERGED when
    all of a ticket's MRs have been merged.
    """
    try:
        merged_mrs = client.list_recently_merged_mrs(username, updated_after=last_sync)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Merged MR fetch failed: {exc}")
        return

    if not merged_mrs:
        return

    merged_urls = {str(mr.get("web_url", "")) for mr in merged_mrs}

    for ticket in Ticket.objects.in_flight():
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        mrs = extra.get("mrs", {})
        if not isinstance(mrs, dict) or not mrs:
            continue

        changed = False
        all_merged = True

        for mr_url, mr_entry in mrs.items():
            if not isinstance(mr_entry, dict):
                continue
            if mr_url in merged_urls:
                if mr_entry.pop("discussions", None) is not None:
                    changed = True
                result.mrs_merged += 1
            else:
                all_merged = False

        if not changed:
            continue

        extra["mrs"] = mrs
        ticket.extra = extra
        update_fields = ["extra"]

        if all_merged and _STATE_ORDER.index(Ticket.State.MERGED) > _STATE_ORDER.index(ticket.state):
            ticket.state = Ticket.State.MERGED
            update_fields.append("state")

        ticket.save(update_fields=update_fields)


def _fetch_issue_labels(client: GitLabAPI, result: SyncResult) -> None:
    """Fetch GitLab issue labels and work item status for tickets with issue URLs."""
    issue_tickets = Ticket.objects.exclude(issue_url="").filter(
        issue_url__regex=r"/-/(issues|work_items)/\d+$",
    )
    for ticket in issue_tickets:
        match = re.search(r"gitlab\.com/(.+?)/-/(?:issues|work_items)/(\d+)", ticket.issue_url)
        if not match:
            continue

        project_path = match.group(1)
        iid = int(match.group(2))
        if iid == 0:
            continue

        is_work_item = "/work_items/" in ticket.issue_url

        project = client.resolve_project(project_path)
        if not project:
            continue

        issue = client.get_issue(project.project_id, iid)
        if not issue:
            continue

        labels = issue.get("labels", [])
        tracker_status = _process_label(labels) if isinstance(labels, list) else None  # type: ignore[arg-type]

        # For work items without Process:: labels, fetch the Status widget via GraphQL
        if not tracker_status and is_work_item:
            wi_status = client.get_work_item_status(project_path, iid)
            if wi_status:
                tracker_status = wi_status

        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        update_fields: list[str] = []

        if tracker_status and extra.get("tracker_status") != tracker_status:
            extra["tracker_status"] = tracker_status
            update_fields.append("extra")

        issue_title = str(issue.get("title", ""))
        if issue_title and extra.get("issue_title") != issue_title:
            extra["issue_title"] = issue_title
            if "extra" not in update_fields:
                update_fields.append("extra")

        variant = _extract_variant(list(labels)) if isinstance(labels, list) else ""
        if variant and ticket.variant != variant:
            ticket.variant = variant
            update_fields.append("variant")

        if update_fields:
            ticket.extra = extra
            ticket.save(update_fields=update_fields)
            result.labels_fetched += 1


def fetch_notion_statuses() -> None:
    """Fetch Notion statuses for in-flight tickets.

    Requires Claude MCP Notion integration. When running outside a Claude
    session, raises NotImplementedError.
    """
    msg = (
        "Notion status sync requires Claude MCP integration. "
        "Use notion-search / notion-fetch MCP tools from a Claude session "
        "to populate ticket.extra['notion_status']."
    )
    raise NotImplementedError(msg)


def _fetch_review_permalinks(result: SyncResult) -> None:
    """Fetch Slack review permalinks for in-flight non-draft MRs.

    Searches the configured review channel for messages containing MR URLs.
    Matching is deterministic: exact MR URL substring match, no AI.
    """
    from teetree.backends.slack import search_review_permalinks  # noqa: PLC0415

    token = getattr(settings, "TEATREE_SLACK_TOKEN", "")
    channel_name = getattr(settings, "TEATREE_REVIEW_CHANNEL", "")
    channel_id = getattr(settings, "TEATREE_REVIEW_CHANNEL_ID", "")
    if not token or not channel_id:
        return

    # Collect non-draft MR URLs that don't already have a review permalink
    mr_urls: list[str] = []
    url_to_ticket: dict[str, tuple[Ticket, str]] = {}

    for ticket in Ticket.objects.in_flight():
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        mrs = extra.get("mrs", {})
        if not isinstance(mrs, dict):
            continue
        for mr_url, mr in mrs.items():
            if not isinstance(mr, dict):
                continue
            if mr.get("draft"):
                continue
            if mr.get("review_permalink"):
                continue
            clean_url = mr_url.rstrip("/").split("#")[0]
            mr_urls.append(clean_url)
            url_to_ticket[clean_url] = (ticket, mr_url)

    if not mr_urls:
        return

    try:
        matches = search_review_permalinks(
            token=token,
            channel_id=channel_id,
            channel_name=channel_name,
            mr_urls=mr_urls,
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Slack review sync: {exc}")
        return

    for match in matches:
        ticket, mr_url = url_to_ticket[match.mr_url]
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        mrs = extra.get("mrs", {})
        if not isinstance(mrs, dict):  # pragma: no cover — defensive against concurrent extra mutation
            continue
        mr = mrs.get(mr_url)
        if not isinstance(mr, dict):  # pragma: no cover — defensive against concurrent extra mutation
            continue
        mr["review_permalink"] = match.permalink
        mr["review_channel"] = match.channel
        extra["mrs"] = mrs
        ticket.extra = extra
        ticket.save(update_fields=["extra"])
        result.reviews_synced += 1


def _extract_variant(labels: list[object]) -> str:
    """Extract a customer/variant name from GitLab issue labels.

    Only returns labels that match TEATREE_KNOWN_VARIANTS (case-insensitive).
    """
    known = getattr(settings, "TEATREE_KNOWN_VARIANTS", [])
    known_lower = {v.lower(): v for v in known}
    for label in labels:
        text = str(label)
        match = known_lower.get(text.lower())
        if match:
            return match
    return ""


def _process_label(labels: list[object]) -> str | None:
    for label in labels:
        text = str(label)
        if text.startswith(("Process::", "Process:: ")):
            return text
    return None


_STATE_ORDER = [s.value for s in Ticket.State]


def _infer_state_from_mrs(mrs_data: dict[str, dict[str, object]]) -> str:
    """Infer minimum ticket state from MR metadata.

    Synced tickets bypass FSM transitions (which have side effects like task
    creation). This returns the furthest state the MR data implies.
    """
    best = Ticket.State.NOT_STARTED
    for mr in mrs_data.values():
        if not isinstance(mr, dict):
            continue
        is_draft = mr.get("draft", True)
        if is_draft:
            candidate = Ticket.State.STARTED
        else:
            approvals = mr.get("approvals")
            has_approvals = isinstance(approvals, dict) and int(approvals.get("count", 0)) > 0  # ty: ignore[no-matching-overload]
            review_requested = bool(mr.get("review_requested"))
            candidate = Ticket.State.IN_REVIEW if (has_approvals or review_requested) else Ticket.State.SHIPPED
        if _STATE_ORDER.index(candidate) > _STATE_ORDER.index(best):
            best = candidate
    return best


_ISSUE_URL_RE = re.compile(r"(https://gitlab\.com/[^\s)]+/-/(?:issues|work_items)/\d+)")


def _extract_issue_url(mr: dict[str, object]) -> str:
    """Extract a GitLab issue URL from MR title or description first line."""
    for text in [
        str(mr.get("description", "") or "").split("\n", maxsplit=1)[0],
        str(mr.get("title", "")),
    ]:
        match = _ISSUE_URL_RE.search(text)
        if match:
            return match.group(1)
    return ""
