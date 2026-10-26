"""The owner note names the held ticket and uses the registered push key."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import Ticket
from teatree.core.shipping_hold_notice import send_shipping_hold_notice


class TestShippingHoldNotice(TestCase):
    def test_note_links_titled_ticket_and_uses_owner_escalation(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/souliane/teatree/issues/5346",
            extra={"issue_title": "Review found no diff"},
        )
        with patch("teatree.core.shipping_hold_notice.notify_user", return_value=True) as notify:
            send_shipping_hold_notice(ticket.pk)
        text = notify.call_args.args[0]
        assert "#5346 (Review found no diff)" in text
        assert ticket.issue_url in text
        assert notify.call_args.kwargs["idempotency_key"] == f"shipping-hold:{ticket.pk}"
        assert notify.call_args.kwargs["audience"] is NotifyAudience.OWNER_ESCALATION
