"""Sync external data (PRs, issues, project boards) into the local database.

Dispatches to the appropriate backend (GitLab or GitHub) based on the
active overlay's configuration. "PR" is the canonical term in core; the
GitLab backend translates to MR internally.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

LAST_SYNC_CACHE_KEY = "teatree_followup_last_sync"
PENDING_REVIEWS_CACHE_KEY = "teatree_pending_reviews"

# Type aliases for untyped external data (GitLab/GitHub API responses)
# and serialized internal data stored in JSONFields.
type RawAPIDict = dict[str, object]
type PREntryDict = dict[str, object]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncResult:
    prs_found: int = 0
    issues_found: int = 0
    tickets_created: int = 0
    tickets_updated: int = 0
    labels_fetched: int = 0
    prs_merged: int = 0
    prs_closed: int = 0
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
class PREntry:
    url: str
    title: str
    branch: str
    draft: bool
    repo: str
    iid: int
    updated_at: str
    state: str = "opened"  # opened | closed | merged | locked — from the upstream PR API
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
    approvals_dismissed_at: str | None = None
    dismissed_approvers: list[str] | None = None

    def to_dict(self) -> PREntryDict:
        result: PREntryDict = {}
        for k in self.__slots__:
            v = getattr(self, k)
            if v is None:
                continue
            if k == "discussions":
                result[k] = [d.to_dict() for d in v]
            else:
                result[k] = v
        return result


class SyncBackend(ABC):
    """Abstract base for code host sync backends.

    Implementations live in ``teatree.backends.*_sync`` and are iterated by
    ``sync_followup()``.  A single backend is responsible for one code host
    (GitHub, GitLab, …).
    """

    @abstractmethod
    def is_configured(self, overlay: object) -> bool:
        """Return True if this backend has valid credentials in *overlay*."""

    @abstractmethod
    def sync(self, overlay: object) -> SyncResult:
        """Sync from this code host into the local database."""


def _merge_results(a: SyncResult, b: SyncResult) -> SyncResult:
    """Merge two SyncResult instances, summing counts and concatenating lists."""
    return SyncResult(
        prs_found=a.prs_found + b.prs_found,
        issues_found=a.issues_found + b.issues_found,
        tickets_created=a.tickets_created + b.tickets_created,
        tickets_updated=a.tickets_updated + b.tickets_updated,
        labels_fetched=a.labels_fetched + b.labels_fetched,
        prs_merged=a.prs_merged + b.prs_merged,
        prs_closed=a.prs_closed + b.prs_closed,
        reviews_synced=a.reviews_synced + b.reviews_synced,
        worktrees_cleaned=a.worktrees_cleaned + b.worktrees_cleaned,
        errors=[*a.errors, *b.errors],
    )


def _overlay_name(overlay: object) -> str:
    """Reverse-lookup the registered name for an overlay instance."""
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

    for name, ov in get_all_overlays().items():
        if ov is overlay:
            return name
    return ""


def sync_followup() -> SyncResult:
    """Sync from all configured code host backends.

    Runs GitHub project board sync and/or GitLab PR sync based on
    which credentials are configured in the active overlay.  When both
    tokens are present, both syncs run and results are merged.
    """
    from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415
    from teatree.backends.gitlab_sync import GitLabSyncBackend  # noqa: PLC0415
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    overlay = get_overlay()
    backends: list[SyncBackend] = [GitHubSyncBackend(), GitLabSyncBackend()]
    result = SyncResult()
    ran_any = False

    for backend in backends:
        if backend.is_configured(overlay):
            try:
                backend_result = backend.sync(overlay)
            except Exception as exc:
                backend_name = type(backend).__name__
                logger.exception("%s sync failed", backend_name)
                backend_result = SyncResult(errors=[f"{backend_name} sync failed: {exc}"])
            result = _merge_results(result, backend_result)
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


__all__ = [
    "LAST_SYNC_CACHE_KEY",
    "PENDING_REVIEWS_CACHE_KEY",
    "DiscussionSummary",
    "PREntry",
    "PREntryDict",
    "RawAPIDict",
    "SyncBackend",
    "SyncResult",
    "_merge_results",
    "_overlay_name",
    "fetch_notion_statuses",
    "sync_followup",
]
