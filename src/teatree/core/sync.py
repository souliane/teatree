"""Sync external data (PRs, issues, project boards) into the local database.

Dispatches to the appropriate backend (GitLab or GitHub) based on the
active overlay's configuration. "PR" is the canonical term in core; the
GitLab backend translates to MR internally.
"""

import logging
import re

from teatree.core.backend_factory import notion_client_from_overlay
from teatree.core.backend_registry import get_backend_provider
from teatree.core.models import Ticket
from teatree.core.models.types import TicketExtra
from teatree.core.overlay_loader import get_all_overlays, get_overlay
from teatree.types import SyncBackend, SyncResult

logger = logging.getLogger(__name__)

_NOTION_PAGE_ID = re.compile(r"[0-9a-fA-F]{32}")


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
        conflicted_mrs=[*a.conflicted_mrs, *b.conflicted_mrs],
    )


def _overlay_name(overlay: object) -> str:
    """Reverse-lookup the registered name for an overlay instance."""
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

    try:
        fetch_notion_statuses()
    except Exception as exc:
        logger.exception("Notion status sync failed")
        result.errors.append(f"Notion status sync failed: {exc}")

    return result


def _notion_page_id(notion_url: str) -> str:
    """Extract the 32-hex page id from a Notion URL (or a bare id)."""
    tail = notion_url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1].replace("-", "")
    matches = _NOTION_PAGE_ID.findall(tail)
    return matches[-1] if matches else ""


def fetch_notion_statuses() -> None:
    """Read each in-flight ticket's Notion page status via the official API.

    Reads the ``notion_status_property`` off every non-terminal ticket that
    carries ``extra['notion_url']`` and records it in ``extra['notion_status']``.
    A clean no-op when no Notion integration token is configured for the active
    overlay — the default-safe posture, identical to no sync at all.
    """
    client = notion_client_from_overlay()
    if client is None:
        logger.debug("Notion status sync skipped — no integration token configured")
        return

    property_name = get_overlay().config.notion_status_property
    for ticket in Ticket.objects.in_flight():
        page_id = _notion_page_id((ticket.extra or {}).get("notion_url", ""))
        if not page_id:
            continue
        status = client.get_page_status(page_id, property_name=property_name)
        if status is not None:
            ticket.merge_extra(set_keys=TicketExtra(notion_status=status))


def push_notion_status(page_id: str, value: str) -> bool:
    """Mirror *value* to a Notion page's status property (teatree → Notion).

    The opt-in WRITE direction: no-op unless the active overlay sets
    ``notion_write_back = True`` (and a token resolves). Returns whether a
    ``PATCH`` was issued.
    """
    config = get_overlay().config
    if not config.notion_write_back:
        return False
    client = notion_client_from_overlay()
    if client is None:
        return False
    client.update_page_status(page_id, property_name=config.notion_status_property, value=value)
    return True
