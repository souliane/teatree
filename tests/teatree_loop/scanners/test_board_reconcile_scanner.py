"""The board-reconcile scanner reports the STATE CHANGES it made, or stays silent.

A janitor whose only evidence is its own success message is indistinguishable from
one that does nothing — this box has a tick that reports ``58 signal(s), 58
action(s)`` while nothing advances. So the scanner emits exactly one signal per
APPLIED transition, naming the ticket, both states and the evidence, and emits
nothing at all on a run that changed nothing.
"""

import contextlib
from collections.abc import Iterator
from unittest.mock import patch

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import Ticket
from teatree.loop.scanners import board_reconcile
from teatree.loop.scanners.board_reconcile import BoardReconcileScanner

_URL = "https://github.com/souliane/teatree/pull/3816"


@contextlib.contextmanager
def _merged_forge(url: str = _URL) -> Iterator[None]:
    """Stand in for the live forge: *url* reads MERGED, anything else UNKNOWN."""

    def _probe(pr_url: str) -> PrOpenState:
        return PrOpenState.MERGED if pr_url == url else PrOpenState.UNKNOWN

    with patch.object(board_reconcile, "pr_open_state", _probe):
        yield


class TestBoardReconcileScanner(TestCase):
    def test_emits_one_signal_per_applied_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.NOT_STARTED, issue_url=_URL)

        with _merged_forge():
            signals = BoardReconcileScanner(overlay_name="t3-teatree").scan()

        assert [s.kind for s in signals] == ["board.reconciled"]
        assert signals[0].payload["ticket_id"] == ticket.pk
        assert signals[0].payload["from_state"] == Ticket.State.NOT_STARTED
        assert signals[0].payload["to_state"] == Ticket.State.MERGED
        assert "forge" in signals[0].payload["reason"]

    def test_a_run_that_changed_nothing_is_silent(self) -> None:
        Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.NOT_STARTED, issue_url=_URL)

        with patch.object(board_reconcile, "pr_open_state", lambda pr_url: PrOpenState.OPEN):
            assert BoardReconcileScanner(overlay_name="t3-teatree").scan() == []

    def test_a_second_consecutive_scan_emits_nothing(self) -> None:
        Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.NOT_STARTED, issue_url=_URL)

        with _merged_forge():
            scanner = BoardReconcileScanner(overlay_name="t3-teatree")
            assert len(scanner.scan()) == 1
            assert scanner.scan() == []

    def test_a_reconcile_failure_never_escapes_the_scan(self) -> None:
        with patch.object(board_reconcile, "reconcile_board", side_effect=RuntimeError("boom")):
            assert BoardReconcileScanner(overlay_name="t3-teatree").scan() == []
