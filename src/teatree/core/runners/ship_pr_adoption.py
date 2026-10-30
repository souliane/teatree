"""Adopt the PR a ship already recorded for its branch instead of opening a second one."""

from collections.abc import Mapping
from typing import TYPE_CHECKING

from teatree.core.merge.pr_create_verify import verify_pr_exists
from teatree.core.merge.pr_url_record import record_pr_url

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import TicketExtra

SHIP_HINT_KEYS = ("pr_title_override", "ship_invoking_branch")


def adopt_recorded_pr_url(ticket: "Ticket", host: "CodeHostBackend", extra: "TicketExtra", branch: str) -> str:
    """The PR URL recorded for *branch* that a retry may adopt, or ``""``.

    #1263: only THIS branch's URL short-circuits — a reused ticket spans one PR per
    workstream. Recorded PRs are adopted offline; only an unverified retry marker
    needs a fresh re-read before it can become a recorded PR (#1194).
    """
    by_branch = extra.get("pr_url_by_branch")
    url = by_branch.get(branch) if isinstance(by_branch, Mapping) else None
    if isinstance(url, str) and url:
        return url
    markers = extra.get("unverified_pr_by_branch")
    url = markers.get(branch) if isinstance(markers, Mapping) else None
    if not isinstance(url, str) or not url:
        return ""
    if not verify_pr_exists(host, url).confirmed:
        return ""
    record_pr_url(ticket, url, branch, pop_keys=SHIP_HINT_KEYS)
    return url
