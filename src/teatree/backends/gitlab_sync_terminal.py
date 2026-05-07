"""Terminal-state MR handling: detect merged and closed MRs, update cached extras.

Split out of ``gitlab_sync.py`` to keep that module under the LOC budget enforced
by ``hooks/check_module_health.py``. The two states share fetch + url-collection
plumbing but diverge on side effects:

- merged → advance the ticket FSM to ``MERGED`` and clean up worktrees
- closed → only rewrite the cached MR state so consumers filter the row out
"""

import logging
from typing import TYPE_CHECKING, cast

import httpx

from teatree.core.cleanup import cleanup_worktree
from teatree.core.models import Ticket, Worktree
from teatree.core.sync import MREntryDict, RawAPIDict, SyncResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.backends.gitlab_api import GitLabAPI

logger = logging.getLogger(__name__)

_STATE_ORDER = [s.value for s in Ticket.State]


def detect_merged_mrs(client: "GitLabAPI", username: str, result: SyncResult, last_sync: str | None) -> None:
    merged_urls = _fetch_terminal_mr_urls(
        client.list_recently_merged_mrs,
        username,
        last_sync,
        result,
        label="Merged",
    )
    if merged_urls is None:
        return
    for ticket in Ticket.objects.in_flight():
        apply_merged_status(ticket, merged_urls, result)


def detect_closed_mrs(client: "GitLabAPI", username: str, result: SyncResult, last_sync: str | None) -> None:
    closed_urls = _fetch_terminal_mr_urls(
        client.list_recently_closed_mrs,
        username,
        last_sync,
        result,
        label="Closed",
    )
    if closed_urls is None:
        return
    for ticket in Ticket.objects.in_flight():
        apply_closed_status(ticket, closed_urls, result)


def apply_merged_status(ticket: Ticket, merged_urls: set[str], result: SyncResult) -> None:
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


def apply_closed_status(ticket: Ticket, closed_urls: set[str], result: SyncResult) -> None:
    # Closed-without-merge has no FSM target and no worktree cleanup: the user may
    # still reopen / push a new MR for the same ticket. Only the cached MR entry is
    # rewritten so the cached state-based filter stops rendering the row.
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    mrs = extra.get("mrs", {})
    if not isinstance(mrs, dict) or not mrs:
        return

    changed = False
    for mr_url, mr_entry in mrs.items():
        if not isinstance(mr_entry, dict) or mr_url not in closed_urls:
            continue
        entry = cast("MREntryDict", mr_entry)
        if entry.pop("discussions", None) is not None:
            changed = True
        if entry.get("state") != "closed":
            entry["state"] = "closed"
            changed = True
        result.mrs_closed += 1

    if changed:
        extra["mrs"] = mrs
        ticket.extra = extra
        ticket.save(update_fields=["extra"])


def _scan_merged_mrs(mrs: RawAPIDict, merged_urls: set[str], result: SyncResult) -> tuple[bool, bool]:
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
        if entry.get("state") != "merged":
            entry["state"] = "merged"
            changed = True
        result.mrs_merged += 1
    return changed, not unmerged


def _cleanup_merged_worktrees(ticket: Ticket, result: SyncResult) -> None:
    for worktree in Worktree.objects.filter(ticket=ticket):
        try:
            cleanup_worktree(worktree)
            result.worktrees_cleaned += 1
        except Exception as exc:
            logger.exception("Failed to clean worktree %s", worktree.repo_path)
            result.errors.append(f"Worktree cleanup failed for {worktree.repo_path} ({worktree.branch}): {exc}")


def _fetch_terminal_mr_urls(
    fetcher: "Callable[..., list[RawAPIDict]]",
    username: str,
    last_sync: str | None,
    result: SyncResult,
    *,
    label: str,
) -> set[str] | None:
    try:
        mrs = fetcher(username, updated_after=last_sync)
    except httpx.HTTPError as exc:
        result.errors.append(f"{label} MR fetch failed: {exc}")
        return None
    if not mrs:
        return None
    return {str(mr.get("web_url", "")) for mr in mrs}
