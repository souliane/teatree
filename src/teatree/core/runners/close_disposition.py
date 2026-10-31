"""Whether a pull request keeps its closing keyword: the ticket's rubric decides, not a default.

A pull request that covers the whole issue keeps ``Closes #N``; one that leaves an acceptance item
without a rubric criterion or owner waiver says ``Relates to #N``, and so does one whose issue cannot be
read. The answer is written to ``Ticket.extra['more_prs_coming']`` where the ship, the preview and the
orphan-branch path all read it.
"""

import logging

from teatree.core.gates.rubric_gate import plan_is_bypassed
from teatree.core.issue_coverage import holds_on_issue, read_ticket_issue, stated_texts, uncovered
from teatree.core.models.ticket import Ticket
from teatree.core.models.types import TicketExtra

logger = logging.getLogger(__name__)


def should_close_ticket(extra: TicketExtra | None, *, setting_enabled: bool) -> bool:
    """Whether the ``Closes/Fixes #N`` keywords stay so the platform closes the issue on merge.

    ``False`` when the overlay setting is off or ``extra['more_prs_coming']`` is set; the keywords are then
    rewritten to ``Relates to``.
    """
    if not setting_enabled:
        return False
    return not bool(extra and extra.get("more_prs_coming"))


def more_prs_coming_for(ticket: Ticket) -> bool:
    if not holds_on_issue(ticket) or plan_is_bypassed(ticket):
        return False
    try:
        items = read_ticket_issue(ticket).items_for(ticket)
    except Exception:
        logger.warning(
            "close disposition: could not read %s; assuming more pull requests are coming",
            ticket.issue_url,
            exc_info=True,
        )
        return True
    return bool(uncovered(items, stated_texts(ticket)))


def record_more_prs_coming(ticket: Ticket) -> TicketExtra:
    """Persist the derived flag and return the ticket's extra as the ship reads it."""
    flag = more_prs_coming_for(ticket)
    ticket.merge_extra(set_keys={"more_prs_coming": flag})
    return TicketExtra(**{**(ticket.extra or {}), "more_prs_coming": flag})
