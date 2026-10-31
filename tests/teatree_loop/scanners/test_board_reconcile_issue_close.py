"""Board reconcile rule F — a pre-ship ticket whose own ISSUE the forge closed (#4711).

The measured wedge: the 2026-08-31 prune retired 226 issues NOT_PLANNED and the board
never learned. Twelve ``Ticket`` rows stayed ``plan_recorded`` behind closed issues and the
coding dispatcher re-offered each forever — one was dispatched eleven times, every cycle
spending a full agent run to re-derive "the owner closed this" and stop. Rules A-E cannot
reach that shape: B/C resolve the URL as a PR (an ``/issues/`` URL is UNKNOWN), D polls
only ``completable_states()`` and asks a COMPLETION predicate, E only DELIVERED.

These lanes drive the real ``get_issue`` seam rather than stubbing rule F's own helper,
so they pin the forge read and the rule together.
"""

import contextlib
import threading
from collections.abc import Callable, Generator
from unittest.mock import patch

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.intake.fix_first import AUTHOR_STATE_PHASE
from teatree.core.models import Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners import board_reconcile, board_reconcile_issue_close
from teatree.loop.scanners.board_reconcile import reconcile_board
from teatree.loop.scanners.board_reconcile_reads import APPLY_MARGIN_SECONDS, READ_WORKERS
from teatree.loop.scanners.board_reconcile_report import BoardAction
from teatree.loop.scanners.scan_deadline import scanner_deadline
from tests._owner_channel import assert_a_plain_card
from tests.teatree_loop._board_reconcile_clock import FakeClock, read_clock

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


class _UrlHost:
    """A forge that records every issue read, closes the URLs in *closed* and runs *on_read* per read."""

    def __init__(self, *, closed: set[str], on_read: Callable[[], None] = lambda: None) -> None:
        self.closed = closed
        self.reads: list[str] = []
        self.answered: list[str] = []
        self._on_read = on_read
        self._lock = threading.Lock()

    def get_issue(self, issue_url: str) -> object:
        with self._lock:
            self.reads.append(issue_url)
        self._on_read()
        with self._lock:
            self.answered.append(issue_url)
        return _NOT_PLANNED if issue_url in self.closed else _OPEN


@contextlib.contextmanager
def _forge_host(host: object) -> Generator[None]:
    """Stand in for the live forge: every issue read goes to *host*, no PR is open."""
    with (
        patch.object(board_reconcile, "pr_open_state", lambda _url: PrOpenState.UNKNOWN),
        patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-teatree": _Overlay()}),
        patch("teatree.backends.issue_reads.get_code_host_for_url", return_value=host),
    ):
        yield


def _forge(payload: object) -> contextlib.AbstractContextManager[None]:
    """Stand in for the live forge: every issue read answers *payload*, no PR is open."""
    return _forge_host(_Host(payload))


class TestClosedIssueRetiresPreShipTicket(TestCase):
    def _ticket(self, *, state: str = Ticket.State.PLAN_RECORDED, url: str = "", **kwargs: object) -> Ticket:
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
            (Ticket.State.PLAN_RECORDED, BoardAction.IGNORED_ISSUE_CLOSED)
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
        assert ticket.state == Ticket.State.PLAN_RECORDED

    def test_an_unreadable_payload_never_retires(self) -> None:
        ticket = self._ticket()

        with _forge({"error": "404"}):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLAN_RECORDED

    def test_a_failed_fetch_never_retires(self) -> None:
        """An unreachable forge must read as UNKNOWN, never as "the owner closed it"."""
        ticket = self._ticket()

        with _forge(RuntimeError("forge unreachable")):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLAN_RECORDED

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
        """Rule D owns a PR_OPENED ticket whose issue is done; rule F must not steal it."""
        ticket = self._ticket(state=Ticket.State.PR_OPENED)

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
        assert ticket.state == Ticket.State.PLAN_RECORDED
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
        assert older.state == Ticket.State.PLAN_RECORDED

    def test_remaining_of_one_lets_rule_e_take_the_only_probe(self) -> None:
        """#4808 FINDING 2: the reservation floor is ``remaining // 2``, which is 0 at remaining==1.

        A rule-E DELIVERED candidate exists alongside this test's own rule-F
        candidate, so — unlike ``test_the_probe_budget_bounds_the_rule`` above,
        which passes only because it has no rule-E candidate — this actually
        exercises the smallest budget where the two rules compete. Rule E runs
        first and the floor guarantees F nothing at ``remaining == 1``: E takes
        the sole probe and F is never even read this run.
        """
        delivered = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.DELIVERED,
            issue_url="https://github.com/souliane/teatree/issues/9001",
        )
        ticket = self._ticket()

        with _forge(_NOT_PLANNED):
            report = reconcile_board(probe_budget=1)

        ticket.refresh_from_db()
        delivered.refresh_from_db()
        assert ticket.state == Ticket.State.PLAN_RECORDED
        assert delivered.state == Ticket.State.DELIVERED
        assert report.probes == 1

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
        assert ticket.is_settled
        assert ticket.state not in AUTHOR_STATE_PHASE

    def test_a_ticket_the_fsm_refuses_is_skipped_not_crashed(self) -> None:
        ticket = self._ticket()

        with _forge(_NOT_PLANNED), patch.object(board_reconcile_issue_close, "can_proceed", return_value=False):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLAN_RECORDED

    def test_rule_f_still_gets_probes_when_rule_e_candidates_exceed_the_remaining_budget(self) -> None:
        """The starvation this pins (#4711 follow-up): E's DELIVERED pool must not eat F's whole share.

        Five rule-E candidates outnumber the tiny budget below, so under the original
        "E spends the whole remainder" split F would have been left with 0 probes.
        """
        for n in range(5):
            Ticket.objects.create(
                overlay="t3-teatree",
                state=Ticket.State.DELIVERED,
                issue_url=f"https://github.com/souliane/teatree/issues/50{n}",
            )
        ticket = self._ticket()

        with _forge(_NOT_PLANNED):
            report = reconcile_board(probe_budget=4)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        assert [t.action for t in report.applied if t.ticket_id == ticket.pk] == [BoardAction.IGNORED_ISSUE_CLOSED]


class TestTheJanitorSpendsItsDeadlineOnTheBoundedRulesFirst(TestCase):
    """Rule D reads every post-ship ticket unbudgeted, so it must come last and the reads must overlap.

    The incident: one pass took 137 s against a 60 s scan deadline because rule D's serial
    reads ran before rules E and F, so the tick abandoned the scanner with nothing applied
    and a ticket behind a closed issue stayed live for hours.
    """

    def _retirable(self, *, url: str = _ISSUE) -> Ticket:
        return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PLAN_RECORDED, issue_url=url)

    def _post_ship(self, count: int) -> list[str]:
        urls = [f"https://github.com/souliane/teatree/issues/7{n:03d}" for n in range(count)]
        Ticket.objects.bulk_create(
            [Ticket(overlay="t3-teatree", state=Ticket.State.PR_OPENED, issue_url=url) for url in urls]
        )
        return urls

    def test_the_closed_issue_is_read_and_retired_before_any_completion_read(self) -> None:
        retired = self._retirable()
        completion_urls = self._post_ship(3)
        host = _UrlHost(closed={_ISSUE})

        with _forge_host(host):
            reconcile_board()

        retired.refresh_from_db()
        assert retired.state == Ticket.State.IGNORED
        assert host.reads[0] == _ISSUE
        assert sorted(host.reads[1:]) == sorted(completion_urls)

    def test_a_deadline_during_the_completion_reads_keeps_the_retirement_and_stops_the_reads(self) -> None:
        scan_deadline_seconds, seconds_per_read = 60.0, 2.0
        retired = self._retirable()
        self._post_ship(40)
        clock = FakeClock()
        host = _UrlHost(closed={_ISSUE}, on_read=lambda: clock.advance(seconds_per_read))

        with _forge_host(host), read_clock(clock), scanner_deadline(clock.now + scan_deadline_seconds):
            reconcile_board()

        retired.refresh_from_db()
        reads_before_the_apply_margin = int((scan_deadline_seconds - APPLY_MARGIN_SECONDS) / seconds_per_read)
        assert retired.state == Ticket.State.IGNORED
        assert len(host.reads) <= reads_before_the_apply_margin + READ_WORKERS
        assert len(host.reads) < scan_deadline_seconds / seconds_per_read

    def test_the_closed_issue_reads_overlap(self) -> None:
        urls = [f"{_ISSUE}{n}" for n in range(2)]
        for url in urls:
            self._retirable(url=url)
        host = _UrlHost(closed=set(urls), on_read=threading.Barrier(2, timeout=5).wait)

        with _forge_host(host):
            reconcile_board()

        assert sorted(host.answered) == urls

    def test_the_completion_reads_overlap(self) -> None:
        urls = self._post_ship(2)
        host = _UrlHost(closed=set(), on_read=threading.Barrier(2, timeout=5).wait)

        with _forge_host(host):
            reconcile_board()

        assert sorted(host.answered) == urls


@contextlib.contextmanager
def _dirty(paths: list[str]) -> Generator[None]:
    """Stand in for the on-disk probe: the ticket's worktrees report *paths* as dirty."""
    with patch(_DIRTY_PROBE, return_value=paths):
        yield


class TestUnshippedWorkIsSurfacedNotVetoed(TestCase):
    """Uncommitted work never blocks the retire — leaving the row planned is the defect."""

    def test_a_dirty_worktree_still_retires_and_raises_exactly_one_question(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PLAN_RECORDED, issue_url=_ISSUE)

        with _forge(_NOT_PLANNED), _dirty(["/checkouts/4084/teatree"]):
            reconcile_board()
            reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        questions = DeferredQuestion.objects.filter(dedupe_marker=f"issue-closed-unshipped-work:{ticket.pk}")
        assert questions.count() == 1
        assert questions.get().evidence["decision"] == "irreversible"
        text = assert_a_plain_card(questions.get(), "/checkouts/4084/teatree", _ISSUE)
        assert "1 working copy" in text

    def test_a_clean_worktree_raises_no_question(self) -> None:
        Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PLAN_RECORDED, issue_url=_ISSUE)

        with _forge(_NOT_PLANNED), _dirty([]):
            reconcile_board()

        assert not DeferredQuestion.objects.filter(dedupe_marker__startswith="issue-closed-unshipped-work:").exists()

    def test_an_unreadable_checkout_never_blocks_the_retirement(self) -> None:
        """A probe this venue cannot complete must not leave the row as a dispatch source."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PLAN_RECORDED, issue_url=_ISSUE)
        with _forge(_NOT_PLANNED), patch(_DIRTY_PROBE, side_effect=OSError("checkout unreadable")):
            assert len(reconcile_board().applied) == 1

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        assert not DeferredQuestion.objects.filter(dedupe_marker__startswith="issue-closed-unshipped-work:").exists()
