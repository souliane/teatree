"""The operator dry-run reaches the settlement pass, reads evidence and writes nothing."""

import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Ticket
from teatree.loop.shipping_hold_evidence import Evidence
from teatree.loop.shipping_hold_settle import Settlement, SettlementPassError


class TestShippingHoldCommands(TestCase):
    def test_claim_refusal_is_reported(self) -> None:
        Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.SELF_REVIEWED,
            extra={"shipping_hold": {"reason": "awaiting evidence", "checked_at": ""}},
        )
        errors = StringIO()
        with patch("teatree.core.managers_task_claim.claim_admission_block_reason", return_value="quiescing"):
            call_command("ticket", "settle-holds", stderr=errors)
        assert "settlement deferred: quiescing" in errors.getvalue()

    def test_settle_holds_reports_applied_rows_after_later_failure(self) -> None:
        output = StringIO()
        errors = StringIO()
        applied = Settlement(12, "hold", "unverified: forge")
        with patch(
            "teatree.core.management.commands._shipping_hold_commands.settle_held_tickets",
            side_effect=SettlementPassError("ticket 13: forge unavailable", [applied]),
        ):
            call_command("ticket", "settle-holds", "--json", stdout=output, stderr=errors)
        assert json.loads(output.getvalue()) == [{"ticket_id": 12, "action": "hold", "reason": "unverified: forge"}]
        assert "ticket 13: forge unavailable" in errors.getvalue()

    def test_held_but_not_due_is_visible_in_human_result(self) -> None:
        Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.SELF_REVIEWED,
            extra={"shipping_hold": {"reason": "awaiting evidence", "checked_at": timezone.now().isoformat()}},
        )
        output = StringIO()
        errors = StringIO()
        call_command("ticket", "settle-holds", stdout=output, stderr=errors)
        assert "0 held ticket(s) checked" in errors.getvalue()
        assert "1 held remaining" in errors.getvalue()

    def test_settle_holds_dry_run_reports_without_writing(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.SELF_REVIEWED,
            issue_url="https://github.com/souliane/teatree/issues/5346",
            extra={
                "shipping_hold": {
                    "reason": "no worktree row",
                    "skipped": "no shippable diff",
                    "held_at": "2026-10-26T00:00:00+00:00",
                    "checked_at": "",
                }
            },
        )
        output = StringIO()
        with (
            patch("teatree.loop.shipping_hold_settle.disk_evidence", return_value=Evidence()),
            patch("teatree.loop.shipping_hold_settle.forge_evidence", return_value=Evidence(found="branch 5346-work")),
        ):
            call_command("ticket", "settle-holds", "--dry-run", "--json", stdout=output)
        assert json.loads(output.getvalue()) == [
            {"ticket_id": ticket.pk, "action": "hold", "reason": "branch 5346-work"}
        ]
        ticket.refresh_from_db()
        assert ticket.extra["shipping_hold"]["checked_at"] == ""
        assert ticket.extra["shipping_hold"]["reason"] == "no worktree row"
