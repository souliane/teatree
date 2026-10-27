"""Owner note sent after a rowless shipping hold commits."""

import logging

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models.ticket import Ticket
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.ref_render import render_ref

logger = logging.getLogger(__name__)


def send_shipping_hold_notice(ticket_pk: int) -> None:
    ticket = Ticket.objects.get(pk=ticket_pk)
    title = str((ticket.extra or {}).get("issue_title") or ticket.short_description or "held ticket")
    label = render_ref(
        f"#{ticket.ticket_number}" if ticket.issue_number else f"TODO-{ticket.pk}",
        title=title,
        url=ticket.issue_url,
    )
    try:
        sent = notify_user(
            f"{label}: review found no worktree row. Shipping is held while the workspace and forge are checked.",
            kind=NotifyKind.INFO,
            idempotency_key=f"shipping-hold:{ticket.pk}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
        if not sent:
            logger.warning("Shipping hold owner note for ticket %s was not sent", ticket.pk)
    except Exception:
        logger.exception("Shipping hold owner note for ticket %s failed", ticket.pk)
