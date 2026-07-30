"""``ticket_backfill_titles`` stamps forge titles onto existing tickets."""

from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase
from django_typer.management import TyperCommand

from teatree.core.management.commands import ticket_backfill_titles as cmd_mod
from teatree.core.management.commands.ticket_backfill_titles import Command
from teatree.core.models import Ticket


def _host_returning(title: str) -> MagicMock:
    host = MagicMock()
    host.get_issue.return_value = {"title": title}
    return host


class TicketBackfillTitlesTests(TestCase):
    def test_command_is_registered(self) -> None:
        assert issubclass(Command, TyperCommand)

    def _run(self, host: MagicMock) -> None:
        with (
            patch.object(cmd_mod, "get_overlay_for_ticket", return_value=MagicMock()),
            patch.object(cmd_mod, "get_code_host_for_url", return_value=host),
        ):
            call_command("ticket_backfill_titles")

    def test_fills_issue_title_and_short_description(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/o/r/issues/9")
        self._run(_host_returning("Real forge title"))
        ticket.refresh_from_db()
        assert ticket.extra["issue_title"] == "Real forge title"
        assert ticket.short_description == "Real forge title"

    def test_skips_synthetic_loop_keys(self) -> None:
        ticket = Ticket.objects.create(issue_url="scanning-news://t3-teatree")
        host = _host_returning("should never be fetched")
        self._run(host)
        ticket.refresh_from_db()
        host.get_issue.assert_not_called()
        assert ticket.short_description == ""

    def test_is_idempotent(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/o/r/issues/9")
        self._run(_host_returning("First title"))
        # A second run with a different title must not clobber the first.
        self._run(_host_returning("Different title"))
        ticket.refresh_from_db()
        assert ticket.extra["issue_title"] == "First title"
        assert ticket.short_description == "First title"

    def test_already_titled_ticket_is_not_refetched(self) -> None:
        Ticket.objects.create(
            issue_url="https://github.com/o/r/issues/9",
            extra={"issue_title": "already here"},
        )
        host = _host_returning("new")
        self._run(host)
        host.get_issue.assert_not_called()
