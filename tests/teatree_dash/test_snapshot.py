"""Canonical-HTML drift test for the ``/dash/board`` page (#3162).

The cheap, non-pixel regression the plan mandates: a fresh render of the board
must byte-match the committed ``snapshots/board.html``. A nav / template / column
regression turns this RED. Regenerate the committed file (below) after an
intended board-markup change and review the diff like any other code.

    uv run python -c "import django; django.setup(); ...; render_board_snapshot()"
"""

from django.test import TestCase

from teatree.dash.dashboard_snapshot import SNAPSHOT_PATH, render_board_snapshot


class BoardSnapshotTestCase(TestCase):
    def test_board_html_matches_committed_snapshot(self) -> None:
        committed = SNAPSHOT_PATH.read_text(encoding="utf-8")
        rendered = render_board_snapshot()
        rendered_normalized = rendered if rendered.endswith("\n") else rendered + "\n"
        assert rendered_normalized == committed, (
            "the /dash/board render drifted from snapshots/board.html — "
            "regenerate the committed snapshot and review the diff"
        )
