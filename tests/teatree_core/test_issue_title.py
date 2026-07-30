"""``core.issue_title`` reads/fetches a forge title; the signal seeds new cards."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, TransactionTestCase

from teatree.core import issue_title as it
from teatree.core.issue_title import fetch_issue_title, read_issue_title
from teatree.core.models import Ticket


class TestReadIssueTitle:
    def test_reads_title_string(self) -> None:
        assert read_issue_title({"title": "Fix the widget"}) == "Fix the widget"

    def test_missing_title_is_blank(self) -> None:
        assert read_issue_title({"number": 5}) == ""

    def test_non_string_title_is_blank(self) -> None:
        assert read_issue_title({"title": 123}) == ""


class TestFetchIssueTitle(TestCase):
    def test_sentinel_url_never_touches_the_forge(self) -> None:
        ticket = Ticket.objects.create(issue_url="scanning-news://t3-teatree")
        with patch.object(it, "get_backend_provider") as provider:
            assert fetch_issue_title(ticket) == ""
        provider.assert_not_called()

    def test_forge_url_reads_title_via_provider(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/o/r/issues/9")
        host = MagicMock()
        host.get_issue.return_value = {"title": "A real title"}
        provider = MagicMock()
        provider.get_code_host_for_url.return_value = host
        with (
            patch.object(it, "get_overlay_for_ticket", return_value=MagicMock()),
            patch.object(it, "get_backend_provider", return_value=provider),
        ):
            assert fetch_issue_title(ticket) == "A real title"

    def test_unresolved_host_is_blank(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/o/r/issues/9")
        provider = MagicMock()
        provider.get_code_host_for_url.return_value = None
        with (
            patch.object(it, "get_overlay_for_ticket", return_value=MagicMock()),
            patch.object(it, "get_backend_provider", return_value=provider),
        ):
            assert fetch_issue_title(ticket) == ""


class TestNewTicketTitleSignal(TransactionTestCase):
    """A newly-created forge ticket populates its card description after commit."""

    def test_description_populates_on_create(self) -> None:
        # In TransactionTestCase (autocommit) the create commits immediately, so
        # the post_save on_commit forge fetch runs synchronously inside create().
        host = MagicMock()
        host.get_issue.return_value = {"title": "Make the dashboard usable"}
        provider = MagicMock()
        provider.get_code_host_for_url.return_value = host
        with (
            patch.object(it, "get_overlay_for_ticket", return_value=MagicMock()),
            patch.object(it, "get_backend_provider", return_value=provider),
        ):
            ticket = Ticket.objects.create(issue_url="https://github.com/o/r/issues/42")
        ticket.refresh_from_db()
        assert ticket.extra["issue_title"] == "Make the dashboard usable"
        assert ticket.short_description == "Make the dashboard usable"

    def test_sentinel_ticket_stays_blank(self) -> None:
        with patch.object(it, "get_backend_provider") as provider:
            ticket = Ticket.objects.create(issue_url="scanning-news://t3-teatree")
        ticket.refresh_from_db()
        assert ticket.short_description == ""
        provider.assert_not_called()
