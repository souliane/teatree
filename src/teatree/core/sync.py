"""Sync external data (MRs/PRs, issues, project boards) into the local database.

Dispatches to the appropriate backend (GitLab or GitHub) based on the
active overlay's configuration.
"""

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, cast

from django.core.cache import cache
from django.utils import timezone

from teatree.core.cleanup import cleanup_worktree
from teatree.core.models import Ticket, Worktree

if TYPE_CHECKING:
    from teatree.backends.gitlab_api import GitLabAPI, ProjectInfo

LAST_SYNC_CACHE_KEY = "teatree_followup_last_sync"
PENDING_REVIEWS_CACHE_KEY = "teatree_pending_reviews"

# Type aliases for untyped external data (GitLab/GitHub API responses)
# and serialized internal data stored in JSONFields.
type RawAPIDict = dict[str, object]
type MREntryDict = dict[str, object]

_REPO_PATH_RE = re.compile(r"https?://[^/]+/(.+?)/-/merge_requests/")

logger = logging.getLogger(__name__)


def _overlay_name(overlay: object) -> str:
    """Reverse-lookup the registered name for an overlay instance."""
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

    for name, ov in get_all_overlays().items():
        if ov is overlay:
            return name
    return ""


@dataclass(slots=True)
class SyncResult:
    mrs_found: int = 0
    tickets_created: int = 0
    tickets_updated: int = 0
    labels_fetched: int = 0
    mrs_merged: int = 0
    reviews_synced: int = 0
    worktrees_cleaned: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DiscussionSummary:
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class MREntry:
    url: str
    title: str
    branch: str
    draft: bool
    repo: str
    iid: int
    updated_at: str
    pipeline_status: str | None = None
    pipeline_url: str | None = None
    approvals: RawAPIDict | None = None
    discussions: list[DiscussionSummary] | None = None
    e2e_test_plan_url: str | None = None
    review_requested: bool | None = None
    reviewer_names: list[str] | None = None
    review_permalink: str | None = None
    review_channel: str | None = None
    notion_status: str | None = None
    notion_url: str | None = None
    draft_comments_pending: bool | None = None
    draft_comments_count: int | None = None

    def to_dict(self) -> MREntryDict:
        result: MREntryDict = {}
        for k in self.__slots__:
            v = getattr(self, k)
            if v is None:
                continue
            if k == "discussions":
                result[k] = [d.to_dict() for d in v]
            else:
                result[k] = v
        return result


def _merge_results(a: SyncResult, b: SyncResult) -> SyncResult:
    """Merge two SyncResult instances, summing counts and concatenating lists."""
    return SyncResult(
        mrs_found=a.mrs_found + b.mrs_found,
        tickets_created=a.tickets_created + b.tickets_created,
        tickets_updated=a.tickets_updated + b.tickets_updated,
        labels_fetched=a.labels_fetched + b.labels_fetched,
        mrs_merged=a.mrs_merged + b.mrs_merged,
        reviews_synced=a.reviews_synced + b.reviews_synced,
        worktrees_cleaned=a.worktrees_cleaned + b.worktrees_cleaned,
        errors=[*a.errors, *b.errors],
    )


def sync_followup() -> SyncResult:
    """Sync from all configured code host backends.

    Runs GitHub project board sync and/or GitLab MR sync based on
    which credentials are configured in the active overlay.  When both
    tokens are present, both syncs run and results are merged.
    """
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    overlay = get_overlay()
    result = SyncResult()
    ran_any = False

    if overlay.config.get_github_token():
        result = _merge_results(result, _sync_github(overlay))
        ran_any = True
    if overlay.config.get_gitlab_token():
        result = _merge_results(result, _sync_gitlab(overlay))
        ran_any = True

    if not ran_any:
        overlay_label = _overlay_name(overlay) or "active overlay"
        result.errors.append(f"No code host token for {overlay_label}")

    return result


# ── GitHub sync ──────────────────────────────────────────────────────


def _sync_github(overlay: object) -> SyncResult:
    """Sync issues from a GitHub Projects v2 board into Tickets."""
    from teatree.backends.github import fetch_project_items  # noqa: PLC0415
    from teatree.core.overlay import OverlayBase  # noqa: PLC0415

    if not isinstance(overlay, OverlayBase):
        return SyncResult(errors=["Invalid overlay"])

    token = overlay.config.get_github_token()
    owner = overlay.config.github_owner
    project_number = overlay.config.github_project_number

    if not owner or not project_number:
        return SyncResult(errors=["GitHub owner or project number not configured"])

    result = SyncResult()

    try:
        items = fetch_project_items(owner, project_number, token=token)
    except Exception as exc:  # noqa: BLE001
        return SyncResult(errors=[f"GitHub project fetch failed: {exc}"])

    status_map = {
        "Todo": Ticket.State.NOT_STARTED,
        "In Progress": Ticket.State.STARTED,
        "Done": Ticket.State.DELIVERED,
    }

    for item in items:
        result.mrs_found += 1
        state = status_map.get(item.status, Ticket.State.NOT_STARTED)
        extra: RawAPIDict = {
            "issue_title": item.title,
            "board_position": item.position,
            "board_status": item.status,
            "labels": item.labels,
            "updated_at": item.updated_at,
        }

        tickets = list(Ticket.objects.filter(issue_url=item.url).order_by("pk"))
        if not tickets:
            Ticket.objects.create(
                issue_url=item.url,
                repos=[owner.split("/")[-1]],
                state=state,
                extra=extra,
            )
            result.tickets_created += 1
        else:
            ticket = tickets[0]
            # Preserve existing extra fields not set by sync
            existing_extra = ticket.extra if isinstance(ticket.extra, dict) else {}
            existing_extra.update(extra)
            ticket.extra = existing_extra
            ticket.state = state
            ticket.save(update_fields=["extra", "state"])
            result.tickets_updated += 1

    _sync_github_reviewer_prs(token, result)

    return result


def _sync_github_reviewer_prs(token: str, result: SyncResult) -> None:
    """Fetch GitHub PRs where user is requested reviewer and cache them."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415, S404

    gh_bin = shutil.which("gh")
    if not gh_bin:
        return

    try:
        out = subprocess.run(  # noqa: S603
            [
                gh_bin,
                "search",
                "prs",
                "--review-requested=@me",
                "--state=open",
                "--json",
                "url,title,repository,number,author,isDraft,updatedAt",
                "--limit",
                "50",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={
                **__import__("os").environ,
                "GH_TOKEN": token,
            },
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"GitHub reviewer PR fetch failed: {exc}")
        return

    if out.returncode != 0:
        return

    import json  # noqa: PLC0415

    try:
        prs = json.loads(out.stdout)
    except json.JSONDecodeError:
        return

    reviews: list[dict[str, str]] = []
    for pr in prs:
        repo = pr.get("repository", {})
        repo_name = repo.get("name", "") if isinstance(repo, dict) else ""
        author = pr.get("author", {})
        author_login = author.get("login", "") if isinstance(author, dict) else ""
        reviews.append(
            {
                "url": str(pr.get("url", "")),
                "title": str(pr.get("title", "")),
                "repo": repo_name,
                "iid": str(pr.get("number", "")),
                "author": author_login,
                "draft": str(pr.get("isDraft", False)),
                "updated_at": str(pr.get("updatedAt", "")),
            }
        )

    # Merge with any GitLab reviews already cached
    existing = cache.get(PENDING_REVIEWS_CACHE_KEY) or []
    existing_urls = {r["url"] for r in existing}
    for r in reviews:
        if r["url"] not in existing_urls:
            existing.append(r)
    cache.set(PENDING_REVIEWS_CACHE_KEY, existing, timeout=None)
    result.reviews_synced += len(reviews)


# ── GitLab sync ──────────────────────────────────────────────────────


def _sync_gitlab(overlay: object) -> SyncResult:
    """Fetch all open MRs for the current user and upsert Tickets.

    Uses the global ``/merge_requests`` endpoint so no per-repo configuration
    is needed.  On subsequent runs, only fetches MRs updated since the last
    successful sync (using GitLab's ``updated_after`` filter).
    """
    from teatree.backends.gitlab_api import GitLabAPI, ProjectInfo  # noqa: PLC0415
    from teatree.core.overlay import OverlayBase  # noqa: PLC0415

    if not isinstance(overlay, OverlayBase):
        return SyncResult(errors=["Invalid overlay"])

    overlay_name = _overlay_name(overlay)
    token = overlay.config.get_gitlab_token()
    client = GitLabAPI(token=token, base_url=overlay.config.gitlab_url)
    username = overlay.config.get_gitlab_username() or client.current_username()
    if not username:
        return SyncResult(errors=["GitLab username is not configured in overlay"])
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
        _upsert_ticket_from_mr(mr, repo_path, client, project, result, username=username, overlay_name=overlay_name)

    # Backfill overlay on any in-flight tickets that still have it empty
    if overlay_name:
        Ticket.objects.in_flight().filter(overlay="").update(overlay=overlay_name)

    _fetch_issue_labels(client, result)
    _detect_merged_mrs(client, username, result, last_sync)
    _fetch_review_permalinks(result)
    _sync_reviewer_mrs(client, username, result)

    cache.set(LAST_SYNC_CACHE_KEY, sync_started_at.isoformat(), timeout=None)

    return result


def _extract_repo_path(mr: RawAPIDict) -> str:
    """Extract the GitLab project path from an MR's ``web_url``."""
    web_url = str(mr.get("web_url", ""))
    match = _REPO_PATH_RE.search(web_url)
    return match.group(1) if match else ""


def _upsert_ticket_from_mr(  # noqa: PLR0913, PLR0914
    mr: RawAPIDict,
    repo_path: str,
    client: "GitLabAPI",
    project: "ProjectInfo | None",
    result: SyncResult,
    *,
    username: str = "",
    overlay_name: str = "",
) -> None:
    issue_url = _extract_issue_url(mr)
    web_url = str(mr.get("web_url", ""))
    title = str(mr.get("title", ""))
    source_branch = str(mr.get("source_branch", ""))
    is_draft = bool(mr.get("draft"))
    mr_iid = int(mr.get("iid", 0))  # type: ignore[arg-type]
    repo_short = repo_path.rsplit("/", maxsplit=1)[-1]

    mr_entry = MREntry(
        url=web_url,
        title=title,
        branch=source_branch,
        draft=is_draft,
        repo=repo_short,
        iid=mr_iid,
        updated_at=str(mr.get("updated_at", "")),
    )

    # Enrich non-draft MRs with pipeline and approval data
    if not is_draft and project and mr_iid:
        pipeline = client.get_mr_pipeline(project.project_id, mr_iid)
        mr_entry.pipeline_status = pipeline["status"]
        mr_entry.pipeline_url = pipeline["url"]

        approvals = client.get_mr_approvals(project.project_id, mr_iid)
        mr_entry.approvals = approvals

        discussions = client.get_mr_discussions(project.project_id, mr_iid)
        mr_entry.discussions = _classify_discussions(discussions, username)

        e2e_url = _detect_e2e_evidence(discussions, web_url)
        if e2e_url:
            mr_entry.e2e_test_plan_url = e2e_url

        draft_count = client.get_draft_notes_count(project.project_id, mr_iid)
        mr_entry.draft_comments_pending = draft_count > 0
        mr_entry.draft_comments_count = draft_count if draft_count > 0 else None

    # Reviewer info is available on all MRs (including drafts)
    reviewers = mr.get("reviewers", [])
    if isinstance(reviewers, list):
        mr_entry.review_requested = bool(reviewers)
        mr_entry.reviewer_names = [str(r.get("username", "")) for r in reviewers if isinstance(r, dict)]  # ty: ignore[no-matching-overload]

    lookup_url = issue_url or web_url
    mr_entry_dict = mr_entry.to_dict()
    inferred_state = _infer_state_from_mrs({web_url: mr_entry_dict})
    tickets = list(Ticket.objects.filter(issue_url=lookup_url).order_by("pk"))
    if not tickets:
        ticket = Ticket.objects.create(
            issue_url=lookup_url,
            repos=[repo_short],
            extra={"mrs": {web_url: mr_entry_dict}},
            state=inferred_state,
            overlay=overlay_name,
        )
        result.tickets_created += 1
    else:
        ticket = tickets[0]
        for dup in tickets[1:]:
            _merge_ticket_extras(ticket, dup)
            dup.delete()
        if overlay_name and not ticket.overlay:
            ticket.overlay = overlay_name
            ticket.save(update_fields=["overlay"])
        _update_ticket(ticket, mr_entry_dict, web_url, repo_short, inferred_state)
        result.tickets_updated += 1


def _classify_discussions(
    discussions: list[RawAPIDict],
    author_username: str,
) -> list[DiscussionSummary]:
    """Classify MR discussion threads into review comment statuses.

    Statuses: "waiting_reviewer", "needs_reply", "addressed".
    """
    result: list[DiscussionSummary] = []
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

        result.append(DiscussionSummary(status=status, detail=first_body[:120]))
    return result


_E2E_EVIDENCE_RE = re.compile(
    r"e2e|test.?evidence|playwright|screenshot|side.by.side|figma",
    re.IGNORECASE,
)


def _detect_e2e_evidence(discussions: list[RawAPIDict], mr_url: str) -> str:
    """Scan MR discussion notes for E2E test evidence. Returns the note URL or empty string."""
    for disc in discussions:
        if not isinstance(disc, dict):
            continue
        notes = disc.get("notes", [])
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not isinstance(note, dict):
                continue
            body = str(note.get("body", ""))  # ty: ignore[no-matching-overload]
            # Check for E2E keywords or embedded images (common in evidence posts)
            has_image = "![" in body or "/uploads/" in body
            has_keyword = bool(_E2E_EVIDENCE_RE.search(body))
            if has_keyword and has_image:
                note_id = note.get("id")  # ty: ignore[invalid-argument-type]
                return f"{mr_url}#note_{note_id}" if note_id else mr_url
    return ""


_SKILL_WRITTEN_FIELDS = ("review_channel", "review_permalink", "e2e_test_plan_url", "notion_status", "notion_url")


def _merge_ticket_extras(target: Ticket, source: Ticket) -> None:
    """Merge MR data and repos from a duplicate ticket into the target."""
    src_extra = source.extra if isinstance(source.extra, dict) else {}
    tgt_extra = target.extra if isinstance(target.extra, dict) else {}

    src_mrs = src_extra.get("mrs", {})
    tgt_mrs = tgt_extra.get("mrs", {})
    if isinstance(src_mrs, dict) and isinstance(tgt_mrs, dict):
        for url, entry in src_mrs.items():
            if url not in tgt_mrs:
                tgt_mrs[url] = entry
        tgt_extra["mrs"] = tgt_mrs
        target.extra = tgt_extra

    src_repos = source.repos if isinstance(source.repos, list) else []
    tgt_repos = target.repos if isinstance(target.repos, list) else []
    for repo in src_repos:
        if repo not in tgt_repos:
            tgt_repos.append(repo)
    target.repos = tgt_repos
    target.save(update_fields=["extra", "repos"])


def _update_ticket(
    ticket: Ticket,
    mr_entry: MREntryDict,
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


def _scan_merged_mrs(
    mrs: RawAPIDict,
    merged_urls: set[str],
    result: SyncResult,
) -> tuple[bool, bool]:
    """Scan MR entries, strip discussions from merged ones. Returns (changed, all_merged)."""
    changed = False
    unmerged = False
    for mr_url, mr_entry in mrs.items():
        if not isinstance(mr_entry, dict):
            continue
        if mr_url not in merged_urls:
            unmerged = True
            continue
        entry = cast("MREntryDict", mr_entry)
        if entry.pop("discussions", None) is not None:
            changed = True
        result.mrs_merged += 1
    return changed, not unmerged


def _apply_merged_status(ticket: Ticket, merged_urls: set[str], result: SyncResult) -> None:
    """Clean stale discussion data and advance ticket state when all MRs are merged."""
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    mrs = extra.get("mrs", {})
    if not isinstance(mrs, dict) or not mrs:
        return

    changed, all_merged = _scan_merged_mrs(mrs, merged_urls, result)

    if not changed and not all_merged:
        return

    update_fields: list[str] = []
    if changed:
        extra["mrs"] = mrs
        ticket.extra = extra
        update_fields.append("extra")
    if all_merged and _STATE_ORDER.index(Ticket.State.MERGED) > _STATE_ORDER.index(ticket.state):
        ticket.state = Ticket.State.MERGED
        update_fields.append("state")
    if update_fields:
        ticket.save(update_fields=update_fields)

    if all_merged:
        _cleanup_merged_worktrees(ticket, result)


def _cleanup_merged_worktrees(ticket: Ticket, result: SyncResult) -> None:
    """Auto-clean worktrees associated with a fully-merged ticket."""
    for worktree in Worktree.objects.filter(ticket=ticket):
        try:
            cleanup_worktree(worktree)
            result.worktrees_cleaned += 1
        except Exception:
            logger.exception("Failed to clean worktree %s", worktree.repo_path)
            result.errors.append(f"Worktree cleanup failed: {worktree.repo_path}")


def _detect_merged_mrs(
    client: "GitLabAPI",
    username: str,
    result: SyncResult,
    last_sync: str | None,
) -> None:
    """Detect MRs merged since last sync and clean up their ticket data."""
    try:
        merged_mrs = client.list_recently_merged_mrs(username, updated_after=last_sync)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Merged MR fetch failed: {exc}")
        return

    if not merged_mrs:
        return

    merged_urls = {str(mr.get("web_url", "")) for mr in merged_mrs}
    for ticket in Ticket.objects.in_flight():
        _apply_merged_status(ticket, merged_urls, result)


_ISSUE_PARTS_RE = re.compile(r"https?://[^/]+/(.+?)/-/(?:issues|work_items)/(\d+)")


def _resolve_issue(client: "GitLabAPI", issue_url: str) -> tuple[dict, str, int] | None:
    """Parse a GitLab issue/work-item URL and fetch the issue. Returns (issue, project_path, iid) or None."""
    import httpx  # noqa: PLC0415

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
        logger.warning("Failed to fetch issue %s#%d: %s", project_path, iid, exc)
        return None
    return (issue, project_path, iid) if issue else None


def _apply_issue_data(client: "GitLabAPI", ticket: Ticket, issue: dict, project_path: str, iid: int) -> bool:
    """Update a ticket with labels, title, variant, and tracker status from an issue. Returns True if saved."""
    labels = issue.get("labels", [])
    tracker_status = _process_label(labels) if isinstance(labels, list) else None

    if not tracker_status and "/work_items/" in ticket.issue_url:
        tracker_status = client.get_work_item_status(project_path, iid) or tracker_status

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
        return True
    return False


def _fetch_issue_labels(client: "GitLabAPI", result: SyncResult) -> None:
    """Fetch GitLab issue labels and work item status for tickets with issue URLs."""
    for ticket in Ticket.objects.exclude(issue_url="").filter(
        issue_url__regex=r"/-/(issues|work_items)/\d+$",
    ):
        resolved = _resolve_issue(client, ticket.issue_url)
        if not resolved:
            continue
        issue, project_path, iid = resolved
        if _apply_issue_data(client, ticket, issue, project_path, iid):
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


def _collect_reviewable_mr_urls() -> tuple[list[str], dict[str, tuple[Ticket, str]]]:
    """Collect non-draft MR URLs without review permalinks from in-flight tickets."""
    mr_urls: list[str] = []
    url_to_ticket: dict[str, tuple[Ticket, str]] = {}
    for ticket in Ticket.objects.in_flight():
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        mrs = extra.get("mrs", {})
        if not isinstance(mrs, dict):
            continue
        for mr_url, mr in mrs.items():
            if not isinstance(mr, dict) or mr.get("draft") or mr.get("review_permalink"):
                continue
            clean_url = mr_url.rstrip("/").split("#")[0]
            mr_urls.append(clean_url)
            url_to_ticket[clean_url] = (ticket, mr_url)
    return mr_urls, url_to_ticket


def _fetch_review_permalinks(result: SyncResult) -> None:
    """Fetch Slack review permalinks for in-flight non-draft MRs."""
    from teatree.backends.slack import search_review_permalinks  # noqa: PLC0415
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    overlay = get_overlay()
    token = overlay.config.get_slack_token()
    channel_name, channel_id = overlay.config.get_review_channel()
    if not token or not channel_id:
        return

    mr_urls, url_to_ticket = _collect_reviewable_mr_urls()
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


def _sync_reviewer_mrs(client: "GitLabAPI", username: str, result: SyncResult) -> None:
    """Fetch MRs where user is reviewer (not author) and cache for dashboard display."""
    try:
        reviewer_mrs = client.list_open_mrs_as_reviewer(username)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Reviewer MR fetch failed: {exc}")
        return

    reviews: list[dict[str, str]] = []
    for mr in reviewer_mrs:
        web_url = str(mr.get("web_url", ""))
        author_info = mr.get("author", {})
        author_name = str(author_info.get("username", "")) if isinstance(author_info, dict) else ""  # ty: ignore[no-matching-overload]
        repo_path = _extract_repo_path(mr)
        repo_short = repo_path.rsplit("/", maxsplit=1)[-1]
        iid = str(mr.get("iid", ""))
        reviews.append(
            {
                "url": web_url,
                "title": str(mr.get("title", "")),
                "repo": repo_short,
                "iid": iid,
                "author": author_name,
                "draft": str(mr.get("draft", False)),
                "updated_at": str(mr.get("updated_at", "")),
            }
        )

    cache.set(PENDING_REVIEWS_CACHE_KEY, reviews, timeout=None)
    result.reviews_synced += len(reviews)


def _extract_variant(labels: list[object]) -> str:
    """Extract a customer/variant name from GitLab issue labels.

    Only returns labels that match overlay known variants (case-insensitive).
    """
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    known = get_overlay().config.known_variants
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


def _infer_state_from_mrs(mrs_data: dict[str, MREntryDict]) -> str:
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


_ISSUE_URL_RE = re.compile(r"(https://[^\s)]+/-/(?:issues|work_items)/\d+)")


def _extract_issue_url(mr: RawAPIDict) -> str:
    """Extract a GitLab issue URL from MR title or description first line."""
    for text in [
        str(mr.get("description", "") or "").split("\n", maxsplit=1)[0],
        str(mr.get("title", "")),
    ]:
        match = _ISSUE_URL_RE.search(text)
        if match:
            return match.group(1)
    return ""
