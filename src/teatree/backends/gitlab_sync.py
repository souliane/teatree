"""GitLab sync backend — MR upsert, issue labels, merged MR cleanup, assigned issues."""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

import httpx
from django.core.cache import cache
from django.utils import timezone

from teatree.backends.gitlab_sync_approvals import detect_approval_dismissal
from teatree.backends.gitlab_sync_terminal import detect_closed_mrs, detect_merged_mrs
from teatree.backends.slack_review_sync import fetch_review_permalinks
from teatree.core.models import Ticket
from teatree.core.sync import (
    LAST_SYNC_CACHE_KEY,
    PENDING_REVIEWS_CACHE_KEY,
    DiscussionSummary,
    MREntry,
    MREntryDict,
    RawAPIDict,
    SyncBackend,
    SyncResult,
    _overlay_name,
)

if TYPE_CHECKING:
    from teatree.backends.gitlab_api import GitLabAPI, ProjectInfo

logger = logging.getLogger(__name__)

_REPO_PATH_RE = re.compile(r"https?://[^/]+/(.+?)/-/merge_requests/")
_ISSUE_PARTS_RE = re.compile(r"https?://[^/]+/(.+?)/-/(?:issues|work_items)/(\d+)")
_ISSUE_URL_RE = re.compile(r"(https://[^\s)]+/-/(?:issues|work_items)/\d+)")
_E2E_EVIDENCE_RE = re.compile(
    r"e2e|test.?evidence|playwright|screenshot|side.by.side|figma",
    re.IGNORECASE,
)
_SKILL_WRITTEN_FIELDS = ("review_channel", "review_permalink", "e2e_test_plan_url", "notion_status", "notion_url")
_STATE_ORDER = [s.value for s in Ticket.State]


@dataclass(frozen=True, slots=True)
class _MRContext:
    mr: RawAPIDict
    repo_short: str
    client: "GitLabAPI"
    project: "ProjectInfo | None"


class GitLabSyncBackend(SyncBackend):
    @override
    def is_configured(self, overlay: object) -> bool:
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415

        return isinstance(overlay, OverlayBase) and bool(overlay.config.get_gitlab_token())

    @override
    def sync(self, overlay: object) -> SyncResult:
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
            repo_path = self._extract_repo_path(mr)
            repo_short = repo_path.rsplit("/", maxsplit=1)[-1]
            project_id = int(mr.get("project_id", 0))  # type: ignore[arg-type]
            project = (
                ProjectInfo(project_id=project_id, path_with_namespace=repo_path, short_name=repo_short)
                if project_id
                else None
            )
            ctx = _MRContext(mr=mr, repo_short=repo_short, client=client, project=project)
            self._upsert_ticket_from_mr(ctx, result, username=username, overlay_name=overlay_name)

        self._fetch_assigned_issues(client, username, result, overlay_name=overlay_name)

        # Backfill overlay on any in-flight tickets that still have it empty
        if overlay_name:
            Ticket.objects.in_flight().filter(overlay="").update(overlay=overlay_name)

        self._fetch_issue_labels(client, result)
        detect_merged_mrs(client, username, result, last_sync)
        detect_closed_mrs(client, username, result, last_sync)
        fetch_review_permalinks(result)
        self._sync_reviewer_mrs(client, username, result)

        cache.set(LAST_SYNC_CACHE_KEY, sync_started_at.isoformat(), timeout=None)

        return result

    @classmethod
    def _extract_repo_path(cls, mr: RawAPIDict) -> str:
        web_url = str(mr.get("web_url", ""))
        match = _REPO_PATH_RE.search(web_url)
        return match.group(1) if match else ""

    @classmethod
    def _build_mr_entry(cls, ctx: "_MRContext", *, username: str) -> MREntry:
        """Build a fully enriched MREntry from a raw MR dict."""
        mr = ctx.mr
        web_url = str(mr.get("web_url", ""))
        is_draft = bool(mr.get("draft"))
        mr_iid = int(mr.get("iid", 0))  # type: ignore[arg-type]

        mr_entry = MREntry(
            url=web_url,
            title=str(mr.get("title", "")),
            branch=str(mr.get("source_branch", "")),
            draft=is_draft,
            repo=ctx.repo_short,
            iid=mr_iid,
            updated_at=str(mr.get("updated_at", "")),
            state=str(mr.get("state", "opened")),
        )

        if not is_draft and ctx.project and mr_iid:
            pipeline = ctx.client.get_mr_pipeline(ctx.project.project_id, mr_iid)
            mr_entry.pipeline_status = pipeline["status"]
            mr_entry.pipeline_url = pipeline["url"]
            mr_entry.approvals = ctx.client.get_mr_approvals(ctx.project.project_id, mr_iid)

            discussions = ctx.client.get_mr_discussions(ctx.project.project_id, mr_iid)
            mr_entry.discussions = cls._classify_discussions(discussions, username)
            e2e_url = cls._detect_e2e_evidence(discussions, web_url)
            if e2e_url:
                mr_entry.e2e_test_plan_url = e2e_url

            current_count = (
                int(mr_entry.approvals.get("count", 0)) if isinstance(mr_entry.approvals, dict) else 0  # ty: ignore[invalid-argument-type]
            )
            dismissal = detect_approval_dismissal(discussions, current_approval_count=current_count)
            if dismissal is not None:
                mr_entry.approvals_dismissed_at = dismissal.at
                mr_entry.dismissed_approvers = dismissal.approvers

            draft_count = ctx.client.get_draft_notes_count(ctx.project.project_id, mr_iid)
            mr_entry.draft_comments_pending = draft_count > 0
            mr_entry.draft_comments_count = draft_count if draft_count > 0 else None

        reviewers = mr.get("reviewers", [])
        if isinstance(reviewers, list):
            mr_entry.review_requested = bool(reviewers)
            mr_entry.reviewer_names = [str(r.get("username", "")) for r in reviewers if isinstance(r, dict)]  # ty: ignore[no-matching-overload]

        return mr_entry

    @classmethod
    def _upsert_ticket_from_mr(
        cls,
        ctx: "_MRContext",
        result: SyncResult,
        *,
        username: str = "",
        overlay_name: str = "",
    ) -> None:
        mr_entry = cls._build_mr_entry(ctx, username=username)
        web_url = mr_entry.url
        lookup_url = cls._extract_issue_url(ctx.mr) or web_url
        mr_entry_dict = mr_entry.to_dict()
        inferred_state = cls._infer_state_from_mrs({web_url: mr_entry_dict})

        tickets = list(Ticket.objects.filter(issue_url=lookup_url).order_by("pk"))
        if not tickets:
            Ticket.objects.create(
                issue_url=lookup_url,
                repos=[ctx.repo_short],
                extra={"mrs": {web_url: mr_entry_dict}},
                state=inferred_state,
                overlay=overlay_name,
            )
            result.tickets_created += 1
        else:
            ticket = tickets[0]
            for dup in tickets[1:]:
                cls._merge_ticket_extras(ticket, dup)
                dup.delete()
            if overlay_name and not ticket.overlay:
                ticket.overlay = overlay_name
                ticket.save(update_fields=["overlay"])
            cls._update_ticket(ticket, mr_entry_dict, web_url, ctx.repo_short, inferred_state)
            result.tickets_updated += 1

    @classmethod
    def _classify_discussions(
        cls,
        discussions: list[RawAPIDict],
        author_username: str,
    ) -> list[DiscussionSummary]:
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
                author_info = last_note.get("author", {}) if isinstance(last_note, dict) else {}  # ty: ignore[no-matching-overload]
                last_author = str(author_info.get("username", "")) if isinstance(author_info, dict) else ""
                status = "waiting_reviewer" if last_author == author_username else "needs_reply"

            result.append(DiscussionSummary(status=status, detail=first_body[:120]))
        return result

    @classmethod
    def _detect_e2e_evidence(cls, discussions: list[RawAPIDict], mr_url: str) -> str:
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
                has_image = "![" in body or "/uploads/" in body
                has_keyword = bool(_E2E_EVIDENCE_RE.search(body))
                if has_keyword and has_image:
                    note_id = note.get("id")  # ty: ignore[invalid-argument-type]
                    return f"{mr_url}#note_{note_id}" if note_id else mr_url
        return ""

    @classmethod
    def _merge_ticket_extras(cls, target: Ticket, source: Ticket) -> None:
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

    @classmethod
    def _update_ticket(
        cls,
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

    @classmethod
    def _fetch_assigned_issues(
        cls,
        client: "GitLabAPI",
        username: str,
        result: SyncResult,
        *,
        overlay_name: str = "",
    ) -> None:
        """Upsert tickets for issues assigned to *username* that have no MR yet.

        Tickets keyed by the same ``issue_url`` are consolidated with MR-based
        tickets so each ticket renders as a single dashboard row.
        """
        try:
            issues = client.list_open_issues_for_assignee(username)
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
            repo_path = cls._extract_issue_repo_path(issue_url)
            repo_short = repo_path.rsplit("/", maxsplit=1)[-1] if repo_path else ""

            existing = Ticket.objects.filter(issue_url=issue_url).first()
            if existing is not None:
                if repo_short and isinstance(existing.repos, list) and repo_short not in existing.repos:
                    existing.repos = [*existing.repos, repo_short]
                    existing.save(update_fields=["repos"])
                    result.tickets_updated += 1
                continue

            Ticket.objects.create(
                issue_url=issue_url,
                repos=[repo_short] if repo_short else [],
                extra={"issue_title": str(issue.get("title", ""))},
                state=Ticket.State.NOT_STARTED,
                overlay=overlay_name,
            )
            result.tickets_created += 1

    @classmethod
    def _extract_issue_repo_path(cls, issue_url: str) -> str:
        match = _ISSUE_PARTS_RE.search(issue_url)
        return match.group(1) if match else ""

    @classmethod
    def _resolve_issue(cls, client: "GitLabAPI", issue_url: str) -> tuple[dict, str, int] | None:
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

    @classmethod
    def _apply_issue_data(cls, client: "GitLabAPI", ticket: Ticket, issue: dict, project_path: str, iid: int) -> bool:
        labels = issue.get("labels", [])
        tracker_status = cls._process_label(labels) if isinstance(labels, list) else None

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

        variant = cls._extract_variant(list(labels)) if isinstance(labels, list) else ""
        if variant and ticket.variant != variant:
            ticket.variant = variant
            update_fields.append("variant")

        if update_fields:
            ticket.extra = extra
            ticket.save(update_fields=update_fields)
            return True
        return False

    @classmethod
    def _fetch_issue_labels(cls, client: "GitLabAPI", result: SyncResult) -> None:
        for ticket in Ticket.objects.exclude(issue_url="").filter(
            issue_url__regex=r"/-/(issues|work_items)/\d+$",
        ):
            resolved = cls._resolve_issue(client, ticket.issue_url)
            if not resolved:
                continue
            issue, project_path, iid = resolved
            if cls._apply_issue_data(client, ticket, issue, project_path, iid):
                result.labels_fetched += 1

    @classmethod
    def _extract_variant(cls, labels: list[object]) -> str:
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        known = get_overlay().config.known_variants
        known_lower = {v.lower(): v for v in known}
        for label in labels:
            text = str(label)
            match = known_lower.get(text.lower())
            if match:
                return match
        return ""

    @classmethod
    def _process_label(cls, labels: list[object]) -> str | None:
        for label in labels:
            text = str(label)
            if text.startswith(("Process::", "Process:: ")):
                return text
        return None

    @classmethod
    def _infer_state_from_mrs(cls, mrs_data: dict[str, MREntryDict]) -> str:
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

    @classmethod
    def _extract_issue_url(cls, mr: RawAPIDict) -> str:
        for text in [
            str(mr.get("description", "") or "").split("\n", maxsplit=1)[0],
            str(mr.get("title", "")),
        ]:
            match = _ISSUE_URL_RE.search(text)
            if match:
                return match.group(1)
        return ""

    @classmethod
    def _sync_reviewer_mrs(cls, client: "GitLabAPI", username: str, result: SyncResult) -> None:
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
            repo_path = cls._extract_repo_path(mr)
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
                },
            )

        cache.set(PENDING_REVIEWS_CACHE_KEY, reviews, timeout=None)
        result.reviews_synced += len(reviews)
