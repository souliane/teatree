"""GitLab sync backend — thin orchestrator.

Delegates PR sync to ``gitlab_sync_prs`` and issue sync to
``gitlab_sync_issues``. Keeps only the ``GitLabSyncBackend`` class
with ``is_configured()``, ``sync()``, and ``_sync_reviewer_prs()``.

GitLab API URLs and method names that hit ``/merge_requests/...``
endpoints keep their GitLab-native naming because they describe the
literal endpoint being called.
"""

import logging
from typing import override

from django.core.cache import cache
from django.utils import timezone

from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab.sync_conflicts import collect_conflicted_mrs
from teatree.backends.gitlab.sync_issues import fetch_assigned_issues, fetch_issue_labels
from teatree.backends.gitlab.sync_prs import _PRContext, extract_repo_path, upsert_ticket_from_pr
from teatree.backends.gitlab.sync_terminal import detect_closed_prs, detect_merged_prs
from teatree.backends.slack.review_sync import fetch_review_permalinks
from teatree.core.models import Ticket
from teatree.core.sync import _overlay_name
from teatree.types import LAST_SYNC_CACHE_KEY, PENDING_REVIEWS_CACHE_KEY, SyncBackend, SyncResult

logger = logging.getLogger(__name__)


class GitLabSyncBackend(SyncBackend):
    @override
    def is_configured(self, overlay: object) -> bool:
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415

        return isinstance(overlay, OverlayBase) and bool(overlay.config.get_gitlab_token())

    @override
    def sync(self, overlay: object) -> SyncResult:
        from teatree.backends.gitlab.api import ProjectInfo  # noqa: PLC0415
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415

        if not isinstance(overlay, OverlayBase):
            return SyncResult(errors=["Invalid overlay"])

        token = overlay.config.get_gitlab_token()
        host = GitLabCodeHost(token=token, base_url=overlay.config.gitlab_url)
        overlay_name = _overlay_name(overlay)
        client = host.client
        username = overlay.config.get_gitlab_username() or host.current_user()
        if not username:
            return SyncResult(errors=["GitLab username is not configured in overlay"])
        result = SyncResult()

        last_sync: str | None = cache.get(LAST_SYNC_CACHE_KEY)
        sync_started_at = timezone.now()

        try:
            raw_prs = host.list_my_prs(author=username, updated_after=last_sync)
        except Exception as exc:  # noqa: BLE001 — a PR-fetch failure is recorded as a sync error, never crashes the sync
            return SyncResult(errors=[f"PR fetch failed: {exc}"])

        for raw in raw_prs:
            result.prs_found += 1
            repo_path = extract_repo_path(raw)
            repo_short = repo_path.rsplit("/", maxsplit=1)[-1]
            project_id = int(raw.get("project_id", 0))  # type: ignore[arg-type]
            project = (
                ProjectInfo(project_id=project_id, path_with_namespace=repo_path, short_name=repo_short)
                if project_id
                else None
            )
            ctx = _PRContext(raw=raw, repo_short=repo_short, client=client, project=project)
            upsert_ticket_from_pr(ctx, result, username=username, overlay_name=overlay_name)

        fetch_assigned_issues(host, username, result, overlay_name=overlay_name)

        if overlay_name:
            Ticket.objects.in_flight().filter(overlay="").update(overlay=overlay_name)

        fetch_issue_labels(client, result)
        detect_merged_prs(client, username, result, last_sync)
        detect_closed_prs(client, username, result, last_sync)
        fetch_review_permalinks(result)
        self._sync_reviewer_prs(host, username, result)
        self._detect_conflicted_prs(host, username, result)

        cache.set(LAST_SYNC_CACHE_KEY, sync_started_at.isoformat(), timeout=None)

        return result

    @classmethod
    def _detect_conflicted_prs(cls, host: GitLabCodeHost, username: str, result: SyncResult) -> None:
        """Re-check every open authored MR's mergeability — never incremental.

        A merge conflict re-arises whenever master advances under an open MR,
        so the conflict set is fetched WITHOUT ``updated_after`` (the
        incremental ticket-upsert fetch above would miss a conflicted MR whose
        ``updated_at`` predates the last sync). Detection only — no rebase,
        no push (#78). A failed fetch is recorded as an error, never fatal.
        """
        try:
            open_prs = host.list_my_prs(author=username)
        except Exception as exc:  # noqa: BLE001 — a conflict-check fetch failure is recorded, never crashes the sync
            result.errors.append(f"Conflict-check PR fetch failed: {exc}")
            return
        collect_conflicted_mrs(open_prs, result)

    @classmethod
    def _sync_reviewer_prs(cls, host: GitLabCodeHost, username: str, result: SyncResult) -> None:
        try:
            reviewer_prs = host.list_review_requested_prs(reviewer=username)
        except Exception as exc:  # noqa: BLE001 — a reviewer-PR fetch failure is recorded, never crashes the sync
            result.errors.append(f"Reviewer PR fetch failed: {exc}")
            return

        reviews: list[dict[str, str]] = []
        for raw in reviewer_prs:
            web_url = str(raw.get("web_url", ""))
            author_info = raw.get("author", {})
            author_name = str(author_info.get("username", "")) if isinstance(author_info, dict) else ""  # ty: ignore[no-matching-overload]
            repo_path = extract_repo_path(raw)
            repo_short = repo_path.rsplit("/", maxsplit=1)[-1]
            iid = str(raw.get("iid", ""))
            reviews.append(
                {
                    "url": web_url,
                    "title": str(raw.get("title", "")),
                    "repo": repo_short,
                    "iid": iid,
                    "author": author_name,
                    "draft": str(raw.get("draft", False)),
                    "updated_at": str(raw.get("updated_at", "")),
                },
            )

        cache.set(PENDING_REVIEWS_CACHE_KEY, reviews, timeout=None)
        result.reviews_synced += len(reviews)
