"""The board scanner settles only ledger rows in its overlay slice."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import PullRequest, Ticket
from teatree.loop.scanners.board_reconcile import BoardReconcileScanner


class TestBoardReconcilePrRows(TestCase):
    def test_scanner_settles_its_closed_row_and_leaves_unknown_and_other_overlay_live(self) -> None:
        rows = []
        for overlay in ("test", "test", "other"):
            ticket = Ticket.objects.create(overlay=overlay)
            rows.append(
                PullRequest.objects.record_opened(
                    ticket=ticket, url=f"https://github.com/example-org/example-repo/pull/{len(rows) + 100}"
                )
            )
        states = {rows[0].url: PrOpenState.CLOSED, rows[1].url: PrOpenState.UNKNOWN}
        with patch("teatree.loop.scanners.board_reconcile.pr_open_state", side_effect=lambda url: states[url]):
            BoardReconcileScanner(overlay_name="test").scan()
        for row in rows:
            row.refresh_from_db()
        assert [row.state for row in rows] == [
            PullRequest.State.CLOSED,
            PullRequest.State.OPEN,
            PullRequest.State.OPEN,
        ]

    def test_live_probe_window_reaches_newer_rows_on_next_tick(self) -> None:
        rows = []
        for number in range(4):
            ticket = Ticket.objects.create(overlay="test")
            rows.append(
                PullRequest.objects.record_opened(
                    ticket=ticket, url=f"https://github.com/example-org/example-repo/pull/{number + 200}"
                )
            )
        seen: list[str] = []

        def read_state(url: str) -> str:
            seen.append(url)
            return PrOpenState.OPEN

        with (
            patch("teatree.loop.scanners.board_reconcile.DEFAULT_PROBE_BUDGET", 2),
            patch("teatree.loop.scanners.board_reconcile.pr_open_state", side_effect=read_state),
        ):
            with patch("time.time", return_value=0):
                BoardReconcileScanner(overlay_name="test").scan()
            with patch("time.time", return_value=3600):
                BoardReconcileScanner(overlay_name="test").scan()
        assert set(seen) == {row.url for row in rows}
