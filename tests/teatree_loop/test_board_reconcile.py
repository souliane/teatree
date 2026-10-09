"""Board reconcile — forge truth drives the ticket FSM, on a cadence (#3841, #3840).

The measured wedge: 205 tickets in ``review_delivered``, 55 in ``not_started`` and
exactly ONE in ``merged`` across 328 rows, with merged PRs rendering in the board's
NOT STARTED column. Nothing reconciled the FSM against the forge — the only merge
driver was the keystone (which never sees an out-of-band merge) and the linked-
``PullRequest``-row sweep (which never sees a ticket that has no PR row, the shape
of tickets 403/404 whose ``issue_url`` IS the merged PR).

These lanes pin the five rules of the single reconciliation path, its dry-run,
its idempotence, its fail-closed probe, and its per-run work bound.
"""

import contextlib
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from teatree.core.backend_protocols import PrMergeState, PrOpenState
from teatree.core.gates import merge_evidence_gate
from teatree.core.models import MergeAudit, MergeClear, PullRequest, Ticket
from teatree.core.models.known_issue import KnownIssue
from teatree.loop.scanners import board_reconcile
from teatree.loop.scanners.board_reconcile import reconcile_board
from teatree.loop.scanners.board_reconcile_report import BoardAction
from tests.teatree_loop._board_reconcile_overlays import overlays_registered, rule_f_inert

_FORTY_HEX = "a" * 40


def _merged_query(sha: str = _FORTY_HEX) -> SimpleNamespace:
    return SimpleNamespace(pr_merge_state=lambda: PrMergeState(state="MERGED", merge_commit_oid=sha))


@contextlib.contextmanager
def _forge(states: dict[str, PrOpenState]) -> Iterator[None]:
    """Stand in for the live forge: URL → open state, UNKNOWN for anything unlisted."""

    def _probe(pr_url: str) -> PrOpenState:
        return states.get(pr_url, PrOpenState.UNKNOWN)

    def _merge_state(ref: object) -> PrMergeState:
        url = f"https://github.com/{ref.slug}/pull/{ref.pr_id}"
        state = states.get(url, PrOpenState.UNKNOWN)
        return PrMergeState(state=state.value, merge_commit_oid=_FORTY_HEX if state is PrOpenState.MERGED else "")

    with (
        patch.object(board_reconcile, "pr_open_state", _probe),
        patch(
            "teatree.core.merge.ci_rollup.CodeHostQuery.for_ref",
            side_effect=lambda ref: SimpleNamespace(pr_merge_state=lambda: _merge_state(ref)),
        ),
    ):
        yield


def _merged_pr(ticket: Ticket, *, produce_evidence: bool = True) -> PullRequest:
    row = PullRequest.objects.create(
        ticket=ticket,
        url=f"https://github.com/souliane/teatree/pull/{ticket.pk}",
        repo="souliane/teatree",
        iid=str(ticket.pk),
        overlay=ticket.overlay,
        state=PullRequest.State.MERGED,
    )
    if produce_evidence:
        with patch(
            "teatree.core.merge.ci_rollup.CodeHostQuery.for_ref",
            return_value=_merged_query(),
        ):
            assert merge_evidence_gate.record_confirmed_forge_merge(ticket)
    return row


def _audit_for(ticket: Ticket) -> None:
    clear = MergeClear.objects.create(
        ticket=ticket,
        pr_id=ticket.pk,
        slug="souliane/teatree",
        reviewed_sha=_FORTY_HEX,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )
    MergeAudit.objects.create(clear=clear, merged_sha=_FORTY_HEX, required_checks_status="green")


class TestMergedPrRowRule(TestCase):
    """Rule A — a linked ``PullRequest`` row in MERGED, no forge call (#3540)."""

    def test_author_not_started_with_merged_pr_reaches_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED)
        _merged_pr(ticket)

        report = reconcile_board(probe_forge=False)

        assert len(report.applied) == 1
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_advances_every_pre_merged_state(self) -> None:
        pre_merged = [
            Ticket.State.NOT_STARTED,
            Ticket.State.SCOPED,
            Ticket.State.WORK_STARTED,
            Ticket.State.PLAN_RECORDED,
            Ticket.State.CODED,
            Ticket.State.TESTED,
            Ticket.State.SELF_REVIEWED,
            Ticket.State.PR_OPENED,
            Ticket.State.REVIEW_REQUESTED,
        ]
        for state in pre_merged:
            _merged_pr(Ticket.objects.create(overlay="test", state=state))

        report = reconcile_board(probe_forge=False)

        assert len(report.applied) == len(pre_merged)
        assert set(Ticket.objects.values_list("state", flat=True)) == {Ticket.State.MERGED}

    def test_ticket_without_a_merged_pr_is_left_alone(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        PullRequest.objects.create(
            ticket=ticket,
            url="https://github.com/souliane/teatree/pull/1",
            repo="souliane/teatree",
            iid="1",
            overlay="test",
            state=PullRequest.State.OPEN,
        )

        assert reconcile_board(probe_forge=False).applied == ()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.NOT_STARTED

    def test_post_merged_states_are_never_dragged_backward(self) -> None:
        for state in (Ticket.State.MERGED, Ticket.State.RETRO_RECORDED, Ticket.State.DELIVERED, Ticket.State.IGNORED):
            _merged_pr(Ticket.objects.create(overlay="test", state=state))

        assert reconcile_board(probe_forge=False).applied == ()
        assert set(Ticket.objects.values_list("state", flat=True)) == {
            Ticket.State.MERGED,
            Ticket.State.RETRO_RECORDED,
            Ticket.State.DELIVERED,
            Ticket.State.IGNORED,
        }

    def test_is_idempotent(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        _merged_pr(ticket)

        assert len(reconcile_board(probe_forge=False).applied) == 1
        assert reconcile_board(probe_forge=False).applied == ()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_gate_on_without_evidence_is_a_fail_closed_skip(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.NOT_STARTED)
        _merged_pr(ticket, produce_evidence=False)

        with (
            contextlib.nullcontext(),
            patch(
                "teatree.core.merge.ci_rollup.CodeHostQuery.for_ref",
                return_value=_merged_query(""),
            ),
        ):
            assert reconcile_board(probe_forge=False).applied == ()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.NOT_STARTED

    def test_persistent_merge_refusal_has_one_known_issue_and_resolves_on_success(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        _merged_pr(ticket)
        with patch(
            "teatree.core.gates.merge_evidence_gate.record_confirmed_forge_merge", side_effect=[False, False, True]
        ):
            assert len(reconcile_board(probe_forge=False).refused) == 1
            assert len(reconcile_board(probe_forge=False).refused) == 1
            issue = KnownIssue.objects.get(fingerprint=f"board-merge-refused:{ticket.pk}")
            assert issue.is_open
            assert issue.auto_resolve is False
            assert KnownIssue.objects.filter(fingerprint=issue.fingerprint).count() == 1
            assert len(reconcile_board(probe_forge=False).applied) == 1
        issue.refresh_from_db()
        assert not issue.is_open

    def test_a_merge_refusal_clears_when_the_ticket_merges_by_another_path(self) -> None:
        # The refusal is pinned against auto-resolve, so only a merge can clear it — whoever merges.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEW_REQUESTED)
        _merged_pr(ticket)
        with patch("teatree.core.gates.merge_evidence_gate.record_confirmed_forge_merge", return_value=False):
            assert len(reconcile_board(probe_forge=False).refused) == 1
        issue = KnownIssue.objects.get(fingerprint=f"board-merge-refused:{ticket.pk}")
        assert issue.is_open

        ticket.mark_merged()
        ticket.save()

        issue.refresh_from_db()
        assert not issue.is_open

    def test_a_failing_refusal_resolution_never_blocks_the_merge(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEW_REQUESTED)
        _merged_pr(ticket)

        def _a_write_the_database_refuses(_ticket_pk: int) -> int:
            Ticket.objects.create(pk=ticket.pk, overlay="test")
            return 0

        with (
            patch.object(KnownIssue.objects, "resolve_merge_refusal", side_effect=_a_write_the_database_refuses),
            self.assertLogs("teatree.core.models.known_issue", level="ERROR") as logged,
        ):
            ticket.mark_merged()
            ticket.save()

        assert Ticket.objects.get(pk=ticket.pk).state == Ticket.State.MERGED
        assert any(f"ticket {ticket.pk}" in line for line in logged.output)

    def test_a_merge_whose_save_fails_leaves_the_refusal_open(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEW_REQUESTED)
        _merged_pr(ticket)
        with patch("teatree.core.gates.merge_evidence_gate.record_confirmed_forge_merge", return_value=False):
            reconcile_board(probe_forge=False)
        save = Ticket.save
        locked = "database is locked"

        def _refused_once_merged(row: Ticket, *args: object, **kwargs: object) -> None:
            if row.state == Ticket.State.MERGED:
                raise OperationalError(locked)
            save(row, *args, **kwargs)

        with (
            patch.object(Ticket, "save", autospec=True, side_effect=_refused_once_merged),
            contextlib.suppress(OperationalError),
        ):
            reconcile_board(probe_forge=False)

        assert Ticket.objects.get(pk=ticket.pk).state == Ticket.State.REVIEW_REQUESTED
        assert KnownIssue.objects.get(fingerprint=f"board-merge-refused:{ticket.pk}").is_open

    def test_a_merge_refusal_stays_open_while_the_ticket_is_unmerged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.PR_OPENED)
        _merged_pr(ticket)
        with patch("teatree.core.gates.merge_evidence_gate.record_confirmed_forge_merge", return_value=False):
            reconcile_board(probe_forge=False)

        ticket.request_review()
        ticket.save()

        assert KnownIssue.objects.get(fingerprint=f"board-merge-refused:{ticket.pk}").is_open

    def test_gate_on_with_merge_audit_advances(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.NOT_STARTED)
        _merged_pr(ticket, produce_evidence=False)
        _audit_for(ticket)

        with contextlib.nullcontext():
            assert len(reconcile_board(probe_forge=False).applied) == 1
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_one_poison_ticket_does_not_abort_the_sweep(self) -> None:
        good = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        _merged_pr(good)
        bad = Ticket.objects.create(overlay="test", state=Ticket.State.WORK_STARTED)
        _merged_pr(bad)

        real = board_reconcile._advance_to_merged

        def _boom(ticket: Ticket, *, reason: str, dry_run: bool) -> object:
            if ticket.pk == bad.pk:
                msg = "boom"
                raise RuntimeError(msg)
            return real(ticket, reason=reason, dry_run=dry_run)

        with patch.object(board_reconcile, "_advance_to_merged", _boom):
            assert len(reconcile_board(probe_forge=False).applied) == 1
        good.refresh_from_db()
        assert good.state == Ticket.State.MERGED


class TestReviewerTicketsNeverBecomeAuthoredMerges(TestCase):
    """A reviewer ticket lands REVIEW_DELIVERED — never MERGED, on any rule.

    ``REVIEW_DELIVERED`` is excluded from the reconcile sources because driving a
    reviewer ticket to MERGED claims teatree authored work it only reviewed. That
    same argument applies to a reviewer ticket sitting in NOT_STARTED, which the
    state-based exclusion does not reach: measured on the live board, 21 of 32
    candidate transitions were ``role=reviewer`` — ghosts that would each have
    enqueued a spurious ``execute_teardown`` and are not undoable through the FSM
    (``reconcile_merged`` has no inverse).
    """

    URL = "https://github.com/souliane/teatree/pull/3291"

    def _reviewer(self, state: str = Ticket.State.NOT_STARTED, url: str = URL) -> Ticket:
        return Ticket.objects.create(overlay="t3-teatree", role=Ticket.Role.REVIEWER, state=state, issue_url=url)

    def test_merged_pr_closes_the_review_instead_of_claiming_a_merge(self) -> None:
        ticket = self._reviewer()

        with _forge({self.URL: PrOpenState.MERGED}):
            report = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_DELIVERED
        assert [t.action for t in report.applied] == [BoardAction.REVIEW_CLOSED]

    def test_closed_pr_closes_the_review_instead_of_ignoring(self) -> None:
        ticket = self._reviewer()

        with _forge({self.URL: PrOpenState.CLOSED}):
            reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_DELIVERED

    def test_a_merged_pr_row_also_closes_the_review(self) -> None:
        """Rule A carries the identical hazard — a reviewer ticket must not reach MERGED."""
        ticket = self._reviewer(url="")
        _merged_pr(ticket)

        report = reconcile_board(probe_forge=False)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_DELIVERED
        assert [t.action for t in report.applied] == [BoardAction.REVIEW_CLOSED]

    def test_the_merged_pr_row_lane_is_idempotent(self) -> None:
        """The first CORRECT application is what creates the loop, so run 2 is the test.

        Rule A excludes MERGED from its candidates but not REVIEW_DELIVERED — the state its
        own reviewer branch targets — and ``mark_review_no_action`` accepts REVIEW_DELIVERED
        as a source (the #1431 self-transition). Every later pass would re-emit an
        `applied` transition with `from_state == to_state`, claiming a change that did
        not happen, and re-run the transition body's pending-reviewing-task consumption.
        On the render path this runs every tick.
        """
        ticket = self._reviewer(url="")
        _merged_pr(ticket)

        assert len(reconcile_board(probe_forge=False).applied) == 1
        assert reconcile_board(probe_forge=False).applied == ()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_DELIVERED

    def test_an_author_ticket_still_reaches_merged(self) -> None:
        """The role branch must not disarm the rule for its real target."""
        author = Ticket.objects.create(
            overlay="t3-teatree", role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED, issue_url=self.URL
        )

        with _forge({self.URL: PrOpenState.MERGED}):
            reconcile_board()

        author.refresh_from_db()
        assert author.state == Ticket.State.MERGED

    def test_a_second_run_is_a_no_op(self) -> None:
        self._reviewer()

        with _forge({self.URL: PrOpenState.MERGED}):
            assert len(reconcile_board().applied) == 1
            assert reconcile_board().applied == ()


class TestForgeMergedRule(TestCase):
    """Rule B — the ticket's OWN ``issue_url`` is a PR the forge says merged.

    The tickets 403/404 shape: ``not_started``, no ``PullRequest`` row at all, and
    an ``issue_url`` pointing straight at the merged PR.
    """

    URL = "https://github.com/souliane/teatree/pull/3816"

    def _ticket(self, state: str = Ticket.State.NOT_STARTED) -> Ticket:
        return Ticket.objects.create(overlay="t3-teatree", state=state, issue_url=self.URL)

    def test_not_started_ticket_whose_pr_merged_reaches_merged(self) -> None:
        ticket = self._ticket()

        with _forge({self.URL: PrOpenState.MERGED}):
            report = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert [t.action for t in report.applied] == [BoardAction.ADVANCED_MERGED]
        assert report.applied[0].from_state == Ticket.State.NOT_STARTED

    def test_open_pr_leaves_the_ticket_alone(self) -> None:
        ticket = self._ticket()

        with _forge({self.URL: PrOpenState.OPEN}):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.NOT_STARTED

    def test_unknown_probe_is_fail_closed(self) -> None:
        """An unreachable forge is inconclusive — never advance on uncertainty."""
        ticket = self._ticket()

        with _forge({}):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.NOT_STARTED

    def test_closed_unmerged_pr_resolves_to_ignored(self) -> None:
        ticket = self._ticket()

        with _forge({self.URL: PrOpenState.CLOSED}):
            report = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        assert [t.action for t in report.applied] == [BoardAction.IGNORED_CLOSED]

    def test_second_consecutive_run_is_a_no_op(self) -> None:
        self._ticket()

        with _forge({self.URL: PrOpenState.MERGED}):
            assert len(reconcile_board().applied) == 1
            assert reconcile_board().applied == ()

    def test_the_closed_pr_lane_is_idempotent(self) -> None:
        ticket = self._ticket()

        with _forge({self.URL: PrOpenState.CLOSED}):
            assert len(reconcile_board().applied) == 1
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED

    def test_an_already_merged_ticket_is_never_dragged_to_ignored(self) -> None:
        """MERGED stays a rule-D candidate, so rule C must not reach it.

        A landed ticket is not abandoned. ``ignore()`` accepts MERGED as a source, so
        only an explicit guard keeps a CLOSED verdict from undoing a merge.
        """
        ticket = self._ticket(state=Ticket.State.MERGED)

        with _forge({self.URL: PrOpenState.CLOSED}):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_dry_run_reports_the_transition_without_writing(self) -> None:
        ticket = self._ticket()

        with _forge({self.URL: PrOpenState.MERGED}):
            report = reconcile_board(dry_run=True)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.NOT_STARTED
        assert [t.to_state for t in report.transitions] == [Ticket.State.MERGED]
        assert report.applied == ()

    def test_terminal_tickets_are_never_pr_probed(self) -> None:
        """Rules B/C spend no PR read on a settled ticket.

        Asserted on the PR probe itself rather than ``report.probes``, which also counts
        rule E's issue reads — and rule E deliberately DOES read a delivered ticket's
        issue, that being the whole release valve (#4152).
        """
        for state in (Ticket.State.DELIVERED, Ticket.State.REVIEW_DELIVERED, Ticket.State.IGNORED):
            Ticket.objects.create(
                overlay="t3-teatree",
                state=state,
                issue_url=f"https://github.com/souliane/teatree/pull/{Ticket.State(state).value}",
            )

        with patch.object(board_reconcile, "pr_open_state", return_value=PrOpenState.UNKNOWN) as pr_probe:
            report = reconcile_board()

        assert pr_probe.call_count == 0
        assert report.transitions == ()

    def test_probe_budget_bounds_the_work_per_run(self) -> None:
        for number in range(5):
            Ticket.objects.create(
                overlay="t3-teatree",
                state=Ticket.State.NOT_STARTED,
                issue_url=f"https://github.com/souliane/teatree/pull/{number}",
            )

        with _forge({}):
            assert reconcile_board(probe_budget=2).probes == 2

    def test_a_non_pr_issue_url_is_not_probed_as_a_pr(self) -> None:
        Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.NOT_STARTED,
            issue_url="https://github.com/souliane/teatree/issues/3841",
        )

        with _forge({}), rule_f_inert():
            assert reconcile_board().probes == 0


class _ClosedIssueHost:
    """A code host whose every issue reads CLOSED — rule F's positive input."""

    def get_issue(self, issue_url: str) -> dict[str, str]:
        return {"state": "closed", "state_reason": "not_planned"}


class TestIssueDoneRule(TestCase):
    """Rule D — the post-ship sweep ``sync-completions`` already carried."""

    URL = "https://github.com/souliane/teatree/issues/3841"

    def test_completable_ticket_with_a_done_issue_advances(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PR_OPENED, issue_url=self.URL)
        PullRequest.objects.record_opened(ticket=ticket, url="https://github.com/souliane/teatree/pull/3841")

        with (
            patch.object(board_reconcile, "_issue_done_urls", return_value={self.URL}),
            patch(
                "teatree.core.merge.ci_rollup.CodeHostQuery.for_ref",
                return_value=_merged_query(),
            ),
        ):
            report = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.RETRO_RECORDED
        assert [t.action for t in report.applied] == [BoardAction.ADVANCED_DELIVERED]

    def test_a_pre_ship_ticket_is_not_advanced_by_a_done_issue(self) -> None:
        """Rule D's walk IS the author ladder, so a pre-ship row must never enter it."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.WORK_STARTED, issue_url=self.URL)

        with patch.object(board_reconcile, "_issue_done_urls", return_value={self.URL}), rule_f_inert():
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED

    def test_a_pre_ship_ticket_behind_a_closed_issue_is_retired_by_rule_f_not_advanced(self) -> None:
        """The successor outcome (#4711): the row leaves the board as IGNORED, never DELIVERED."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.WORK_STARTED, issue_url=self.URL)

        with (
            _forge({}),
            patch.object(board_reconcile, "_issue_done_urls", return_value={self.URL}),
            overlays_registered(),
            patch(
                "teatree.backends.issue_reads.get_code_host_for_url",
                return_value=_ClosedIssueHost(),
            ),
        ):
            report = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        assert [t.action for t in report.applied] == [BoardAction.IGNORED_ISSUE_CLOSED]

    def test_the_issue_done_lane_is_idempotent(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.PR_OPENED, issue_url=self.URL)
        PullRequest.objects.record_opened(ticket=ticket, url="https://github.com/souliane/teatree/pull/3841")

        with (
            patch.object(board_reconcile, "_issue_done_urls", return_value={self.URL}),
            patch(
                "teatree.core.merge.ci_rollup.CodeHostQuery.for_ref",
                return_value=_merged_query(),
            ),
        ):
            assert len(reconcile_board().applied) == 1
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.RETRO_RECORDED

    def test_a_reviewer_ticket_is_never_walked_through_merged(self) -> None:
        """The third path to the ghost: ``advance_to_delivered`` is the AUTHOR ladder.

        Its walk goes ``pr_opened → review_requested → merged → retro_recorded``, so a reviewer
        ticket admitted here transits MERGED exactly like rules A/B/C would have —
        equally irreversible. Reachability is currently zero only because GitHub's
        issue parser rejects a ``/pull/`` URL, which is a URL-shape coincidence in a
        GitHub-only install, not a role boundary.
        """
        for state in (Ticket.State.PR_OPENED, Ticket.State.REVIEW_REQUESTED, Ticket.State.MERGED):
            reviewer = Ticket.objects.create(
                overlay="t3-teatree",
                role=Ticket.Role.REVIEWER,
                state=state,
                issue_url=f"https://github.com/souliane/teatree/issues/900{Ticket.State(state).value}",
            )
            with patch.object(board_reconcile, "_issue_done_urls", return_value={reviewer.issue_url}):
                report = reconcile_board()

            reviewer.refresh_from_db()
            assert reviewer.state == state, f"reviewer ticket moved from {state}"
            assert report.applied == ()


class TestReportIsObservable(TestCase):
    """The janitor must report WHAT it changed and WHY, independent of the tick's own message."""

    URL = "https://github.com/souliane/teatree/pull/3815"

    def test_lines_name_the_ticket_the_states_and_the_reason(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.NOT_STARTED, issue_url=self.URL)

        with _forge({self.URL: PrOpenState.MERGED}):
            lines = reconcile_board().lines()

        assert any(
            f"#{ticket.pk}" in line and "not_started" in line and "merged" in line and "forge" in line for line in lines
        ), lines

    def test_a_run_with_nothing_to_do_says_so(self) -> None:
        assert reconcile_board(probe_forge=False).lines() == ["Board already reconciled — nothing to advance."]
