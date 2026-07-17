"""``_check_marker_jam`` — the `t3 doctor` intake-budget jam warning (#3275)."""

import django.test

from teatree.cli.doctor.checks_loop import _check_marker_jam
from teatree.core.models import Ticket
from tests.factories import ImplementedIssueMarkerFactory, TicketFactory


class TestMarkerJamDoctorCheck(django.test.TestCase):
    def test_no_markers_pass(self) -> None:
        assert _check_marker_jam() is True

    def test_live_marker_passes(self) -> None:
        url = "https://github.com/o/r/issues/1"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.CODED)
        ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)
        assert _check_marker_jam() is True

    def test_orphaned_terminal_ticket_marker_warns(self) -> None:
        url = "https://github.com/o/r/issues/2"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.MERGED)
        ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)
        assert _check_marker_jam() is False
