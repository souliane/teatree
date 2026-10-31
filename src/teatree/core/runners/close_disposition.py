"""Whether a pull request keeps its closing keyword: the ticket's rubric decides, not a default.

A pull request that covers the whole issue keeps ``Closes #N``; one that leaves an acceptance item
without a rubric criterion or owner waiver says ``Relates to #N``, and so does one on an umbrella issue,
one whose issue cannot be read and one the ticket declares partial by hand. The answer is written to
``Ticket.extra['more_prs_coming']`` where the ship, the preview and the orphan-branch path all read it;
``more_prs_coming_derived`` marks a flag the derivation raised, so it drops once the rubric covers the
issue while a hand-set one stays.
"""

import logging

from teatree.core.gates.issue_coverage import holds_on_issue, read_ticket_issue, stated_texts, uncovered
from teatree.core.gates.rubric_gate import plan_is_bypassed
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


def _declared_by_hand(ticket: Ticket) -> bool:
    extra = ticket.extra or {}
    return bool(extra.get("more_prs_coming")) and not extra.get("more_prs_coming_derived")


def _derived_from_issue(ticket: Ticket) -> bool:
    if not holds_on_issue(ticket):
        return False
    try:
        issue = read_ticket_issue(ticket)
    except Exception:
        logger.warning(
            "close disposition: could not read %s; assuming more pull requests are coming",
            ticket.issue_url,
            exc_info=True,
        )
        return True
    if issue.umbrella(ticket):
        return True
    return not plan_is_bypassed(ticket) and bool(uncovered(issue.items_for(ticket), stated_texts(ticket)))


def more_prs_coming_for(ticket: Ticket) -> bool:
    return _declared_by_hand(ticket) or _derived_from_issue(ticket)


def record_more_prs_coming(ticket: Ticket) -> TicketExtra:
    """Persist the effective flag and return the ticket's extra as the ship reads it."""
    declared = _declared_by_hand(ticket)
    flag = declared or _derived_from_issue(ticket)
    raised = {"more_prs_coming": flag, "more_prs_coming_derived": flag and not declared}
    ticket.merge_extra(set_keys=TicketExtra(**raised))
    return TicketExtra(**{**(ticket.extra or {}), **raised})
