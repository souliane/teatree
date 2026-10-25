"""The board-reconcile scanner reports state changes except unaudited merges.

A janitor whose only evidence is its own success message is indistinguishable from
one that does nothing — this box has a tick that reports ``58 signal(s), 58
action(s)`` while nothing advances. Audited transitions carry the ticket, both
states and the evidence; unaudited merge transitions are left to the owner alert.
"""

import contextlib
from collections.abc import Generator
from unittest.mock import patch

from django.test import TestCase

from teatree.core.backend_protocols import PrMergeState, PrOpenState
from teatree.core.models import MergeAudit, MergeClear, Ticket
from teatree.loop.domain_jobs import _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners import board_reconcile
from teatree.loop.scanners.board_reconcile import BoardReconcileScanner
from tests.teatree_loop._reopened_tickets import merged_ticket, reopened_ticket

_URL = "https://github.com/souliane/teatree/pull/3816"


@contextlib.contextmanager
def _merged_forge(url: str = _URL) -> Generator[None]:
    """Stand in for the live forge: *url* reads MERGED, anything else UNKNOWN."""

    def _probe(pr_url: str) -> PrOpenState:
        return PrOpenState.MERGED if pr_url == url else PrOpenState.UNKNOWN

    with (
        patch.object(board_reconcile, "pr_open_state", _probe),
        patch(
            "teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state",
            return_value=PrMergeState(state="MERGED", merge_commit_oid="a" * 40),
        ),
    ):
        yield


class TestBoardReconcileScanner(TestCase):
    def test_unaudited_reconcile_does_not_emit_a_routine_merged_notice(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.NOT_STARTED, issue_url=_URL)

        with _merged_forge():
            signals = BoardReconcileScanner(overlay_name="t3-teatree").scan()

        assert signals == []
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_audited_reconcile_emits_one_signal_per_applied_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.NOT_STARTED, issue_url=_URL)
        clear = MergeClear.objects.create(ticket=ticket, pr_id=3816, slug="souliane/teatree", reviewed_sha="a" * 40)
        MergeAudit.objects.create(clear=clear, merged_sha="b" * 40, repo_slug="souliane/teatree")

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
            assert scanner.scan() == []
            assert scanner.scan() == []

    def test_a_reopened_ticket_is_not_reverted_by_the_hourly_scan(self) -> None:
        reopened = reopened_ticket()
        control = merged_ticket(state=Ticket.State.NOT_STARTED)

        signals = BoardReconcileScanner().scan()

        assert [s.payload["ticket_id"] for s in signals] == [control.pk]
        reopened.refresh_from_db()
        assert reopened.state == Ticket.State.WORK_STARTED

    def test_a_reconcile_failure_reaches_the_tick_error_surface(self) -> None:
        job = _ScannerJob(scanner=BoardReconcileScanner(overlay_name="t3-teatree"), overlay="")
        with patch.object(board_reconcile, "reconcile_board", side_effect=RuntimeError("boom")):
            _, signals, error = _run_job(job)
        assert (signals, error) == ([], "RuntimeError: boom")
