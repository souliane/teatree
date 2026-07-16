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
