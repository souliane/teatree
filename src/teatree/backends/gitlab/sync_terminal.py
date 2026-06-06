"""Terminal-state PR handling: detect merged and closed PRs, update cached extras.

Split out of ``gitlab_sync.py`` to keep that module under the LOC budget enforced
by ``hooks/check_module_health.py``. The two states share fetch + url-collection
plumbing but diverge on side effects:

- merged → advance the ticket FSM to ``MERGED`` and clean up worktrees
- closed → only rewrite the cached PR state so consumers filter the row out

GitLab API method names that end in ``_mrs`` are kept (they describe the
literal GitLab endpoint being called); teatree-canonical names use ``pr``.
"""

import logging
from typing import TYPE_CHECKING, cast

import httpx

from teatree.core.cleanup import cleanup_worktree
from teatree.core.gates.dod_gate import record_terminal_dod_violation
from teatree.core.models import Ticket, Worktree
from teatree.types import PREntryDict, RawAPIDict, SyncResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.backends.gitlab.api import GitLabAPI
    from teatree.core.models.types import TicketExtra, TicketSiblingFields

logger = logging.getLogger(__name__)

_STATE_ORDER = [s.value for s in Ticket.State]


def detect_merged_prs(client: "GitLabAPI", username: str, result: SyncResult, last_sync: str | None) -> None:
    merged_urls = _fetch_terminal_pr_urls(
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


def detect_closed_prs(client: "GitLabAPI", username: str, result: SyncResult, last_sync: str | None) -> None:
    closed_urls = _fetch_terminal_pr_urls(
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
    prs = extra.get("prs", {})
    if not isinstance(prs, dict) or not prs:
        return

    changed, all_merged = _scan_merged_prs(prs, merged_urls, result)

    if not changed and not all_merged:
        return

    set_keys = cast("TicketExtra", {"prs": prs}) if changed else None
    also_set: TicketSiblingFields = {}
    advancing_to_merged = all_merged and _STATE_ORDER.index(Ticket.State.MERGED) > _STATE_ORDER.index(ticket.state)
    if advancing_to_merged:
        also_set["state"] = Ticket.State.MERGED
    if set_keys or also_set:
        # #800 N3: canonical locked RMW; extra (prs) + optional state
        # one atomic write via also_set (no split, no unlocked clobber).
        ticket.merge_extra(set_keys=set_keys, also_set=also_set or None)

    if advancing_to_merged:
        # #1426: MERGED reflects a real external merge, so the sync follows
        # reality rather than demoting (which would make the ticket lie). When
        # the DoD local-E2E gate was unmet, the gap is recorded as a durable
        # audit marker + loud log instead of being silently bypassed.
        record_terminal_dod_violation(ticket, Ticket.State.MERGED)

    if all_merged:
        _cleanup_merged_worktrees(ticket, result)


def apply_closed_status(ticket: Ticket, closed_urls: set[str], result: SyncResult) -> None:
    # Closed-without-merge has no FSM target and no worktree cleanup: the user may
    # still reopen / push a new PR for the same ticket. Only the cached PR entry is
    # rewritten so the cached state-based filter stops rendering the row.
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    prs = extra.get("prs", {})
    if not isinstance(prs, dict) or not prs:
        return

    changed = False
    for pr_url, pr_entry in prs.items():
        if not isinstance(pr_entry, dict) or pr_url not in closed_urls:
            continue
        entry = cast("PREntryDict", pr_entry)
        if entry.pop("discussions", None) is not None:
            changed = True
        if entry.get("state") != "closed":
            entry["state"] = "closed"
            changed = True
        result.prs_closed += 1

    if changed:
        # #800 N3: canonical locked RMW (was an unlocked extra save).
        ticket.merge_extra(set_keys=cast("TicketExtra", {"prs": prs}))


def _scan_merged_prs(prs: RawAPIDict, merged_urls: set[str], result: SyncResult) -> tuple[bool, bool]:
    changed = False
    unmerged = False
    for pr_url, pr_entry in prs.items():
        if not isinstance(pr_entry, dict):
            continue
        if pr_url not in merged_urls:
            unmerged = True
            continue
        entry = cast("PREntryDict", pr_entry)
        if entry.pop("discussions", None) is not None:
            changed = True
        if entry.get("state") != "merged":
            entry["state"] = "merged"
            changed = True
        result.prs_merged += 1
    return changed, not unmerged


def _cleanup_merged_worktrees(ticket: Ticket, result: SyncResult) -> None:
    for worktree in Worktree.objects.filter(ticket=ticket):
        try:
            cleanup_result = cleanup_worktree(worktree)
            result.worktrees_cleaned += 1
            result.errors.extend(cleanup_result.errors)
        except Exception as exc:
            logger.exception("Failed to clean worktree %s", worktree.repo_path)
            result.errors.append(f"Worktree cleanup failed for {worktree.repo_path} ({worktree.branch}): {exc}")


def _fetch_terminal_pr_urls(
    fetcher: "Callable[..., list[RawAPIDict]]",
    username: str,
    last_sync: str | None,
    result: SyncResult,
    *,
    label: str,
) -> set[str] | None:
    try:
        raw_prs = fetcher(username, updated_after=last_sync)
    except httpx.HTTPError as exc:
        result.errors.append(f"{label} PR fetch failed: {exc}")
        return None
    if not raw_prs:
        return None
    return {str(raw.get("web_url", "")) for raw in raw_prs}
