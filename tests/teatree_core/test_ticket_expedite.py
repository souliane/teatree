"""The expedite / release-blocker ticket flag: model, CLI, statusline chip (PR-07)."""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands._ticket_show import TicketShowResult, render_ticket_show
from teatree.core.models import Ticket
from teatree.loop.rendering_classification import ActiveTicketRow
from teatree.loop.rendering_zones import _render_ticket_line


class TestExpediteModel(TestCase):
    def test_defaults_false(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        assert ticket.expedited is False
        assert ticket.may_push_before_ci() is False

    def test_may_push_before_ci_reflects_flag(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", expedited=True)
        assert ticket.may_push_before_ci() is True


class TestExpediteCommand(TestCase):
    def test_expedite_sets_the_flag(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        result = cast("dict[str, object]", call_command("ticket", "expedite", str(ticket.pk)))
        assert result["expedited"] is True
        ticket.refresh_from_db()
        assert ticket.expedited is True

    def test_expedite_off_clears_the_flag(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", expedited=True)
        result = cast("dict[str, object]", call_command("ticket", "expedite", str(ticket.pk), off=True))
        assert result["expedited"] is False
        ticket.refresh_from_db()
        assert ticket.expedited is False

    def test_expedite_missing_ticket_exits(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "expedite", "999999")

    def test_show_surfaces_the_expedite_chip(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", expedited=True)
        result = cast("dict[str, object]", call_command("ticket", "show", str(ticket.pk)))
        assert result["expedited"] is True

    def test_show_omits_chip_for_normal_ticket(self) -> None:
        rendered = render_ticket_show(
            TicketShowResult(
                ticket_id=1,
                state="in_review",
                overlay="t3-teatree",
                issue_url="",
                expedited=False,
                phases=[],
            ),
        )
        assert "expedite" not in rendered


class TestExpediteStatuslineChip:
    def test_chip_renders_for_expedited_ticket(self) -> None:
        line = _render_ticket_line(
            "t3-teatree",
            [ActiveTicketRow(number="42", state="coded", issue_url="", title="topic", expedite=True)],
            {},
            colorize=False,
        )
        assert "⚡#42" in line

    def test_no_chip_for_normal_ticket(self) -> None:
        line = _render_ticket_line(
            "t3-teatree",
            [ActiveTicketRow(number="42", state="coded", issue_url="", title="topic", expedite=False)],
            {},
            colorize=False,
        )
        assert "⚡" not in line
        assert "#42" in line
