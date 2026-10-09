"""`t3 ticket dod-override` — the #88 DoD local-E2E gate escape hatch CLI.

Records an explicit, audited override on ``Ticket.extra['dod_e2e_override']``
so a UI-visible ticket the heuristic mis-flags can ship without a local-stack
E2E. A blank reason is refused — a silent bypass is exactly what #88
forecloses.
"""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket


class TicketDodOverrideTest(TestCase):
    def test_records_override_reason_on_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "dod-override", str(ticket.pk), "--reason", "non-UI config change", "--by", "ada"),
        )
        ticket.refresh_from_db()
        override = ticket.extra["dod_e2e_override"]
        assert override["reason"] == "non-UI config change"
        assert override["by"] == "ada"
        assert override["at"]  # timestamp recorded
        assert result["ticket_id"] == int(ticket.pk)
        assert result["reason"] == "non-UI config change"

    def test_blank_reason_exits_nonzero_and_records_nothing(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2")
        with pytest.raises(SystemExit):
            call_command("ticket", "dod-override", str(ticket.pk), "--reason", "   ")
        ticket.refresh_from_db()
        assert "dod_e2e_override" not in (ticket.extra or {})

    def test_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "dod-override", "999999", "--reason", "x")
