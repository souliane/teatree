"""Sync external data (PRs, issues, project boards) into the local database.

Dispatches to the appropriate backend (GitLab or GitHub) based on the
active overlay's configuration. "PR" is the canonical term in core; the
GitLab backend translates to MR internally.
"""

import logging

from teatree.types import SyncBackend, SyncResult

logger = logging.getLogger(__name__)


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
    from teatree.core.backend_registry import get_backend_provider  # noqa: PLC0415
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    overlay = get_overlay()
    backends: list[SyncBackend] = get_backend_provider().build_sync_backends()
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
