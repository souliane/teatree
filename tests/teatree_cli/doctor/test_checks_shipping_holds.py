"""Every held ticket is visible; a failed scan is explicitly unverified."""

from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from django.test import TestCase

from teatree.cli.doctor.checks_shipping_holds import check_shipping_holds
from teatree.core.models import Ticket


class TestShippingHoldsDoctor(TestCase):
    def test_all_held_tickets_are_named(self) -> None:
        for index in range(12):
            Ticket.objects.create(
                overlay="test",
                role=Ticket.Role.AUTHOR,
                state=Ticket.State.SELF_REVIEWED,
                issue_url=f"https://github.com/souliane/teatree/issues/{6000 + index}",
                extra={
                    "shipping_hold": {"reason": f"reason {index}", "skipped": "x", "held_at": "x", "checked_at": ""}
                },
            )
        output = StringIO()
        with redirect_stdout(output):
            assert check_shipping_holds() is True
        lines = output.getvalue().splitlines()
        assert len(lines) == 12
        assert "reason 11" in lines[-1]

    def test_unreadable_scan_reports_unverified(self) -> None:
        output = StringIO()
        with (
            patch("teatree.core.models.shipping_hold.held_author_tickets", side_effect=RuntimeError("offline")),
            redirect_stdout(output),
        ):
            assert check_shipping_holds() is True
        assert "Shipping-hold scan UNVERIFIED" in output.getvalue()
