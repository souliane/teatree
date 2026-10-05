"""Board reconcile rule E — a DELIVERED ticket whose upstream issue the forge says was REOPENED (#4152).

DELIVERED is terminal and a ticket owns its issue URL in every state but IGNORED, so a reopened
issue behind one reached no path at all. These lanes pin the revival, its cap and the one
escalation at the cap, the fail-closed probe, and rule E's share of the per-run probe budget.
"""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.backend_protocols import IssueReopenState
from teatree.core.models import Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners import board_reconcile_issue_reopen as issue_reopen
from teatree.loop.scanners.board_reconcile import reconcile_board
from teatree.loop.scanners.board_reconcile_report import BoardAction
from tests.teatree_loop._board_reconcile_overlays import overlays_registered, rule_f_inert


class TestReopenedIssueRule(TestCase):
    """Rule E — DELIVERED is terminal, so a reopened issue behind it was stranded (#4152)."""

    URL = "https://github.com/souliane/teatree/issues/4133"

    def _delivered(self, *, url: str = "", **kwargs: object) -> Ticket:
        return Ticket.objects.create(
            overlay="t3-teatree", state=Ticket.State.DELIVERED, issue_url=url or self.URL, **kwargs
        )

    def test_a_delivered_ticket_behind_a_reopened_issue_is_revived(self) -> None:
        ticket = self._delivered()

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=({self.URL}, 1)), rule_f_inert():
            report = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
        assert [t.action for t in report.applied] == [BoardAction.REVIVED_REOPENED]

    def test_a_delivered_ticket_whose_issue_is_not_reopened_is_left_alone(self) -> None:
        """The false-positive population — delivered, issue never closed — must not be re-run."""
        ticket = self._delivered()

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=(set(), 0)):
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED

    def test_the_reopened_lane_is_idempotent(self) -> None:
        ticket = self._delivered()

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=({self.URL}, 1)), rule_f_inert():
            assert len(reconcile_board().applied) == 1
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED

    def test_a_reviewer_ticket_is_never_revived(self) -> None:
        """A reviewer ticket's ``issue_url`` IS a PR — rules B/C own it, not this one."""
        reviewer = self._delivered(role=Ticket.Role.REVIEWER)

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=({self.URL}, 1)), rule_f_inert():
            assert reconcile_board().applied == ()

        reviewer.refresh_from_db()
        assert reviewer.state == Ticket.State.DELIVERED

    def test_dry_run_reports_the_revival_without_writing(self) -> None:
        ticket = self._delivered()

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=({self.URL}, 1)), rule_f_inert():
            report = reconcile_board(dry_run=True)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert [t.to_state for t in report.transitions] == [Ticket.State.WORK_STARTED]
        assert report.applied == ()

    def _walk_back_to_delivered(self, ticket: Ticket) -> None:
        """Return a revived ticket to DELIVERED the way the ladder does.

        ``test()`` is the step that matters: it rewrites ``extra`` through
        ``validated_ticket_extra``, so a counter it does not know is dropped here.
        """
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.CODED)
        ticket.refresh_from_db()
        ticket.test(passed=True)
        ticket.save()
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.DELIVERED)
        ticket.refresh_from_db()

    def _capped(self) -> Ticket:
        ticket = self._delivered()
        Ticket.objects.filter(pk=ticket.pk).update(extra={"reopen_revivals": issue_reopen.MAX_REOPEN_REVIVALS})
        ticket.refresh_from_db()
        self._walk_back_to_delivered(ticket)
        return ticket

    def test_the_cap_fires_after_max_revivals_across_ladder_walks(self) -> None:
        """A revival only recurs after a full ladder walk, so the counter must survive one (#4152)."""
        ticket = self._delivered()
        applied = 0

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=({self.URL}, 1)), rule_f_inert():
            for _ in range(issue_reopen.MAX_REOPEN_REVIVALS + 2):
                applied += len(reconcile_board().applied)
                ticket.refresh_from_db()
                self._walk_back_to_delivered(ticket)

        assert applied == issue_reopen.MAX_REOPEN_REVIVALS
        assert DeferredQuestion.objects.filter(dedupe_marker=f"reopen-revival-capped:{ticket.pk}").count() == 1

    def test_the_revival_cap_halts_and_escalates_exactly_once(self) -> None:
        """The cap must not become the silence this rule exists to remove."""
        ticket = self._capped()

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=({self.URL}, 1)), rule_f_inert():
            assert reconcile_board().applied == ()
            assert reconcile_board().applied == ()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert DeferredQuestion.objects.filter(dedupe_marker=f"reopen-revival-capped:{ticket.pk}").count() == 1

    def test_an_answered_escalation_is_never_re_asked(self) -> None:
        """Answering does not move the ticket off DELIVERED, so a per-PENDING guard would re-ask hourly."""
        ticket = self._capped()

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=({self.URL}, 1)), rule_f_inert():
            reconcile_board()
            DeferredQuestion.objects.update(answered_at=timezone.now())
            reconcile_board()

        assert DeferredQuestion.objects.filter(dedupe_marker=f"reopen-revival-capped:{ticket.pk}").count() == 1

    def test_the_live_probe_path_revives_on_a_definite_reopened_verdict(self) -> None:
        ticket = self._delivered()

        with (
            overlays_registered(),
            patch.object(issue_reopen, "issue_reopen_state", return_value=IssueReopenState.REOPENED) as probe,
        ):
            report = reconcile_board()

        assert probe.call_args.args[1] == self.URL
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
        assert [t.action for t in report.applied] == [BoardAction.REVIVED_REOPENED]

    def test_an_unknown_verdict_never_revives(self) -> None:
        """Fail-CLOSED: only a DEFINITE reopened counts, so an unreachable forge is inert."""
        ticket = self._delivered()

        with (
            overlays_registered(),
            patch.object(issue_reopen, "issue_reopen_state", return_value=IssueReopenState.UNKNOWN) as probe,
        ):
            assert reconcile_board().applied == ()

        assert probe.call_count == 1
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED

    def test_the_probe_budget_bounds_the_reopen_reads(self) -> None:
        for n in range(3):
            self._delivered(url=f"https://github.com/souliane/teatree/issues/70{n}")

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=(set(), 0)) as urls:
            reconcile_board(probe_budget=1)

        assert len(urls.call_args.args[0]) == 1

    def test_rule_e_is_not_capped_when_rule_f_has_no_candidates(self) -> None:
        """#4808: the reservation is demand-aware — F's cap can't exceed its own candidates.

        Ten DELIVERED candidates outnumber the ten-probe budget and rule F is
        completely inert (no pre-ship tickets at all). A FIXED half-split
        (``remaining // 2``) would still hand F a reservation of 5 it has nothing
        to spend on and cap rule E at the other 5, stranding half the budget —
        measured at 5 probes/10 on the pre-fix split versus all 10 here.
        """
        for n in range(10):
            self._delivered(url=f"https://github.com/souliane/teatree/issues/60{n}")

        with patch.object(issue_reopen, "_reopened_issue_urls", return_value=(set(), 0)) as urls:
            reconcile_board(probe_budget=10)

        assert len(urls.call_args.args[0]) == 10

    def test_a_url_no_overlay_owns_is_never_charged_as_a_probe(self) -> None:
        """#4808 FINDING 3 (pre-existing): the reported spend counts reads ISSUED, not candidates considered.

        Rule F already had this guarantee (``_closed_issue_verdicts``); rule E's
        ``reopened_issue_transitions`` used to charge ``len(probed)`` regardless of
        whether any read was actually issued, so a ticket whose overlay was not
        installed here was charged for a read that never happened.
        """
        self._delivered()

        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={}):
            report = reconcile_board()

        assert (report.applied, report.probes) == ((), 0)
