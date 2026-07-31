"""``t3 loop reclaim-markers`` frees stranded issue-implementer budget on demand (#3275)."""

import json
from unittest.mock import patch

from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli.loop import loop_app
from teatree.cli.loop.reclaim_markers import reclaim_markers_command
from teatree.core.models import ImplementedIssueMarker, Ticket
from tests.factories import ImplementedIssueMarkerFactory, TicketFactory

runner = CliRunner()


def test_command_is_registered_flat_on_loop_app() -> None:
    registered = {cmd.callback for cmd in loop_app.registered_commands}
    assert reclaim_markers_command in registered


class TestReclaimMarkersCommand(TestCase):
    def setUp(self) -> None:
        patcher = patch("teatree.cli.loop.reclaim_markers.ensure_django")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _jammed_marker(self, url: str, overlay: str = "acme"):
        TicketFactory(overlay=overlay, issue_url=url, state=Ticket.State.MERGED)
        return ImplementedIssueMarkerFactory(overlay=overlay, issue_url=url)

    def test_releases_terminal_ticket_marker_and_reports(self) -> None:
        marker = self._jammed_marker("https://github.com/o/r/issues/1")

        result = runner.invoke(loop_app, ["reclaim-markers", "--overlay", "acme"])

        assert result.exit_code == 0, result.stdout
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED
        assert "Reclaimed 1 stale issue-marker(s)" in result.stdout

    def test_json_output(self) -> None:
        marker = self._jammed_marker("https://github.com/o/r/issues/2")

        result = runner.invoke(loop_app, ["reclaim-markers", "--json"])

        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["released"] == 1
        assert payload["completed"] == [marker.pk]
        assert payload["abandoned"] == []

    def test_nothing_to_reclaim_reports_zero(self) -> None:
        result = runner.invoke(loop_app, ["reclaim-markers"])

        assert result.exit_code == 0, result.stdout
        assert "Reclaimed 0 stale issue-marker(s)" in result.stdout

    def test_a_fresh_ticketless_claim_is_held_by_the_default_grace(self) -> None:
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url="https://github.com/o/r/issues/3")

        result = runner.invoke(loop_app, ["reclaim-markers", "--overlay", "acme"])

        assert result.exit_code == 0, result.stdout
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED

    def test_a_zero_orphan_grace_frees_a_claim_stranded_moments_ago(self) -> None:
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url="https://github.com/o/r/issues/4")

        result = runner.invoke(loop_app, ["reclaim-markers", "--overlay", "acme", "--orphan-grace-hours", "0"])

        assert result.exit_code == 0, result.stdout
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.ABANDONED

    def test_an_explicit_stall_grace_frees_a_ticket_that_stopped_moving(self) -> None:
        url = "https://github.com/o/r/issues/5"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.PLANNED)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True)

        result = runner.invoke(loop_app, ["reclaim-markers", "--overlay", "acme", "--stall-grace-hours", "0"])

        assert result.exit_code == 0, result.stdout
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.ABANDONED


class TestDeadGraceOption(TestCase):
    """``--dead-grace-hours`` governs a claim with nothing queued and no open PR (#3978)."""

    def setUp(self) -> None:
        patcher = patch("teatree.cli.loop.reclaim_markers.ensure_django")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_zero_dead_grace_frees_a_pr_less_claim_stranded_moments_ago(self) -> None:
        url = "https://github.com/o/r/issues/8"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.PLANNED)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True)

        result = runner.invoke(loop_app, ["reclaim-markers", "--overlay", "acme", "--dead-grace-hours", "0"])

        assert result.exit_code == 0, result.stdout
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.ABANDONED

    def test_a_negative_dead_grace_is_refused(self) -> None:
        url = "https://github.com/o/r/issues/9"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.STARTED)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True)

        result = runner.invoke(loop_app, ["reclaim-markers", "--overlay", "acme", "--dead-grace-hours", "-48"])

        assert result.exit_code != 0, result.stdout
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.TICKET_CREATED


class TestNegativeGraceIsRejected(TestCase):
    """A negative grace inverts the cutoff, so this command would abandon LIVE claims.

    Freeing the double-dispatch guard is this command's entire job, so a cutoff
    pushed into the FUTURE releases a claim that is still in flight — and the
    issue is then dispatched a second time, which is exactly what the marker
    exists to prevent. Every grace is bounded at zero so the mistake is a usage
    error, not a silent ``Reclaimed 1``.
    """

    def setUp(self) -> None:
        patcher = patch("teatree.cli.loop.reclaim_markers.ensure_django")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_negative_stall_grace_is_refused(self) -> None:
        url = "https://github.com/o/r/issues/6"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.STARTED)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True)

        result = runner.invoke(loop_app, ["reclaim-markers", "--overlay", "acme", "--stall-grace-hours", "-48"])

        assert result.exit_code != 0, result.stdout
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.TICKET_CREATED

    def test_a_negative_orphan_grace_is_refused(self) -> None:
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url="https://github.com/o/r/issues/7")

        result = runner.invoke(loop_app, ["reclaim-markers", "--overlay", "acme", "--orphan-grace-hours", "-48"])

        assert result.exit_code != 0, result.stdout
        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED
