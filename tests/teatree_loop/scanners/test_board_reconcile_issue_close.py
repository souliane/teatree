"""Board reconcile rule F — a pre-ship ticket whose own ISSUE the forge closed (#4711).

The measured wedge: the 2026-08-31 prune retired 226 issues NOT_PLANNED and the board
never learned. Twelve ``Ticket`` rows stayed ``planned`` behind closed issues and the
coding dispatcher re-offered each forever — one was dispatched eleven times, every cycle
spending a full agent run to re-derive "the owner closed this" and stop. Rules A-E cannot
reach that shape: B/C resolve the URL as a PR (an ``/issues/`` URL is UNKNOWN), D polls
only ``completable_states()`` and asks a COMPLETION predicate, E only DELIVERED.

These lanes drive the real ``get_issue`` seam rather than stubbing rule F's own helper,
so they pin the forge read and the rule together.
"""

import contextlib
from collections.abc import Iterator
from unittest.mock import patch

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop import stuck_ticket_redispatch
from teatree.loop.scanners import board_reconcile, board_reconcile_issue_close
from teatree.loop.scanners.board_reconcile import reconcile_board
from teatree.loop.scanners.board_reconcile_report import BoardAction

_ISSUE = "https://github.com/souliane/teatree/issues/4084"
_DIRTY_PROBE = "teatree.core.models.ticket_worktree_checks.collect_dirty_worktree_paths"
_NOT_PLANNED = {"state": "closed", "state_reason": "not_planned"}
_COMPLETED = {"state": "closed", "state_reason": "completed"}
_OPEN = {"state": "open", "state_reason": None}


class _Host:
    """The one ``CodeHostBackend`` seam every issue read goes through."""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def get_issue(self, issue_url: str) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Overlay:
    """Enough of ``OverlayBase`` for rules D and F to judge a payload."""

    def is_issue_done(self, issue_data: dict) -> bool:
        return issue_data.get("state") in {"closed", "completed"}


@contextlib.contextmanager
def _forge(payload: object) -> Iterator[None]:
    """Stand in for the live forge: every issue read answers *payload*, no PR is open."""
    with (
        patch.object(board_reconcile, "pr_open_state", lambda _url: PrOpenState.UNKNOWN),
        patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-teatree": _Overlay()}),
        patch("teatree.backends.issue_reads.get_code_host_for_url", return_value=_Host(payload)),
    ):
        yield


class TestClosedIssueRetiresPreShipTicket(TestCase):
    def _ticket(self, *, state: str = Ticket.State.PLANNED, url: str = "", **kwargs: object) -> Ticket:
        return Ticket.objects.create(overlay="t3-teatree", state=state, issue_url=url or _ISSUE, **kwargs)

    def test_the_pk632_shape_is_retired_carrying_the_close_reason(self) -> None:
        """#4711's regression row: planned, author, issue CLOSED not_planned."""
        ticket = self._ticket()

        with _forge(_NOT_PLANNED):
            report = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        assert ticket.extra["issue_close_reason"] == "not_planned"
        assert [(t.from_state, t.action) for t in report.applied] == [
            (Ticket.State.PLANNED, BoardAction.IGNORED_ISSUE_CLOSED)
        ]

    def test_a_completed_close_retires_the_row_too_and_stays_distinguishable(self) -> None:
        """Nothing shipped, so DELIVERED would claim a delivery that never happened."""
        ticket = self._ticket(state=Ticket.State.CODED)

        with _forge(_COMPLETED):
            assert len(reconcile_board().applied) == 1

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        assert ticket.extra["issue_close_reason"] == "completed"

    def test_a_close_with_no_reason_still_retires(self) -> None:
        """GitLab marks no ``state_reason``; an absent reason never weakens the verdict."""
        ticket = self._ticket()

        with _forge({"state": "closed"}):
            assert len(reconcile_board().applied) == 1

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        assert ticket.extra["issue_close_reason"] == ""

    def test_an_open_issue_leaves_the_row_alone(self) -> None:
        ticket = self._ticket()

        with _forge(_OPEN):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED

    def test_an_unreadable_payload_never_retires(self) -> None:
        ticket = self._ticket()

        with _forge({"error": "404"}):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED

    def test_a_failed_fetch_never_retires(self) -> None:
        """An unreachable forge must read as UNKNOWN, never as "the owner closed it"."""
        ticket = self._ticket()

        with _forge(RuntimeError("forge unreachable")):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED

    def test_the_lane_is_idempotent(self) -> None:
        ticket = self._ticket()

        with _forge(_NOT_PLANNED):
            assert len(reconcile_board().applied) == 1
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED

    def test_a_reviewer_ticket_is_never_retired(self) -> None:
        """A reviewer ticket's ``issue_url`` IS a PR — rules B/C own it, not this one."""
        reviewer = self._ticket(role=Ticket.Role.REVIEWER)

        with _forge(_NOT_PLANNED):
            assert [t.action for t in reconcile_board().applied] != [BoardAction.IGNORED_ISSUE_CLOSED]

        reviewer.refresh_from_db()
        assert reviewer.state != Ticket.State.IGNORED

    def test_a_post_ship_ticket_stays_with_the_completion_rule(self) -> None:
        """Rule D owns a SHIPPED ticket whose issue is done; rule F must not steal it."""
        ticket = self._ticket(state=Ticket.State.SHIPPED)

        with _forge(_COMPLETED):
            actions = [t.action for t in reconcile_board().transitions]

        ticket.refresh_from_db()
        assert BoardAction.IGNORED_ISSUE_CLOSED not in actions
        assert ticket.state != Ticket.State.IGNORED

    def test_dry_run_reports_the_retirement_without_writing(self) -> None:
        ticket = self._ticket()

        with _forge(_NOT_PLANNED):
            report = reconcile_board(dry_run=True)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED
        assert [t.to_state for t in report.transitions] == [Ticket.State.IGNORED]
        assert report.applied == ()

    def test_the_probe_budget_bounds_the_rule(self) -> None:
        """Newest-first and capped, so one run can never saturate the box.

        Rules B/C spend nothing here: ``forge_of`` reads an ``/issues/<n>`` URL as
        UNKNOWN, so the whole budget reaches rule F.
        """
        older = self._ticket(url=f"{_ISSUE}0")
        newest = self._ticket()

        with _forge(_NOT_PLANNED):
            report = reconcile_board(probe_budget=1)

        older.refresh_from_db()
        newest.refresh_from_db()
        assert len(report.applied) == 1
        assert report.probes == 1
        assert newest.state == Ticket.State.IGNORED
        assert older.state == Ticket.State.PLANNED

    def test_a_url_no_overlay_owns_is_never_charged_as_a_probe(self) -> None:
        """The reported spend counts reads ISSUED, not candidates considered."""
        self._ticket()

        with _forge(_NOT_PLANNED), patch("teatree.core.overlay_loader.get_all_overlays", return_value={}):
            report = reconcile_board()

        assert (report.applied, report.probes) == ((), 0)

    def test_a_retired_row_leaves_the_dispatch_selector(self) -> None:
        """#4711 acceptance: the coding dispatcher can no longer admit the row."""
        ticket = self._ticket()

        with _forge(_NOT_PLANNED):
            reconcile_board()

        ticket.refresh_from_db()
        assert ticket.is_terminal
        assert ticket.state not in stuck_ticket_redispatch._STATE_PHASE

    def test_a_ticket_the_fsm_refuses_is_skipped_not_crashed(self) -> None:
        ticket = self._ticket()

        with _forge(_NOT_PLANNED), patch.object(board_reconcile_issue_close, "can_proceed", return_value=False):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED


@contextlib.contextmanager
def _dirty(paths: list[str]) -> Iterator[None]:
    """Stand in for the on-disk probe: the ticket's worktrees report *paths* as dirty."""
    with patch(_DIRTY_PROBE, return_value=paths):
        yield


class TestUnshippedWorkIsSurfacedNotVetoed(TestCase):
    """Uncommitted work never blocks the retire — leaving the row planned is the defect."""

    def test_a_dirty_worktree_still_retires_and_raises_exactly_one_question(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PLANNED, issue_url=_ISSUE)

        with _forge(_NOT_PLANNED), _dirty(["/checkouts/4084/teatree"]):
            reconcile_board()
            reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        questions = DeferredQuestion.objects.filter(dedupe_marker=f"issue-closed-unshipped-work:{ticket.pk}")
        assert questions.count() == 1
        assert "/checkouts/4084/teatree" in questions.get().question

    def test_a_clean_worktree_raises_no_question(self) -> None:
        Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PLANNED, issue_url=_ISSUE)

        with _forge(_NOT_PLANNED), _dirty([]):
            reconcile_board()

        assert not DeferredQuestion.objects.filter(dedupe_marker__startswith="issue-closed-unshipped-work:").exists()

    def test_an_unreadable_checkout_never_blocks_the_retirement(self) -> None:
        """A probe this venue cannot complete must not leave the row as a dispatch source."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PLANNED, issue_url=_ISSUE)
        with _forge(_NOT_PLANNED), patch(_DIRTY_PROBE, side_effect=OSError("checkout unreadable")):
            assert len(reconcile_board().applied) == 1

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        assert not DeferredQuestion.objects.filter(dedupe_marker__startswith="issue-closed-unshipped-work:").exists()
