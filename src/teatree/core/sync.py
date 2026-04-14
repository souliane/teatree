"""Sync external data (MRs/PRs, issues, project boards) into the local database.

Dispatches to the appropriate backend (GitLab or GitHub) based on the
active overlay's configuration.
"""

import logging
import re
from dataclasses import asdict, dataclass, field

LAST_SYNC_CACHE_KEY = "teatree_followup_last_sync"
PENDING_REVIEWS_CACHE_KEY = "teatree_pending_reviews"

# Type aliases for untyped external data (GitLab/GitHub API responses)
# and serialized internal data stored in JSONFields.
type RawAPIDict = dict[str, object]
type MREntryDict = dict[str, object]

_REPO_PATH_RE = re.compile(r"https?://[^/]+/(.+?)/-/merge_requests/")

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Re-exports from submodules — keeps the public API stable for callers that
# import directly from ``teatree.core.sync`` (including test suite).
# These must come after the type definitions above so the partially-loaded
# module already has SyncResult/MREntry/etc. when the submodules import them.
# ---------------------------------------------------------------------------

from teatree.core.sync_github import (  # noqa: E402
    _sync_github,
    _sync_github_reviewer_prs,
)
from teatree.core.sync_gitlab import (  # noqa: E402
    _apply_merged_status,
    _classify_discussions,
    _collect_reviewable_mr_urls,
    _detect_e2e_evidence,
    _extract_issue_url,
    _extract_variant,
    _fetch_review_permalinks,
    _infer_state_from_mrs,
    _merge_ticket_extras,
    _overlay_name,
    _process_label,
    _resolve_issue,
    _sync_gitlab,
    _sync_reviewer_mrs,
    _update_ticket,
)

__all__ = [
    "LAST_SYNC_CACHE_KEY",
    "PENDING_REVIEWS_CACHE_KEY",
    "DiscussionSummary",
    "MREntry",
    "MREntryDict",
    "RawAPIDict",
    "SyncResult",
    "_apply_merged_status",
    "_classify_discussions",
    "_collect_reviewable_mr_urls",
    "_detect_e2e_evidence",
    "_extract_issue_url",
    "_extract_variant",
    "_fetch_review_permalinks",
    "_infer_state_from_mrs",
    "_merge_results",
    "_merge_ticket_extras",
    "_overlay_name",
    "_process_label",
    "_resolve_issue",
    "_sync_github",
    "_sync_github_reviewer_prs",
    "_sync_gitlab",
    "_sync_reviewer_mrs",
    "_update_ticket",
    "fetch_notion_statuses",
    "sync_followup",
]
