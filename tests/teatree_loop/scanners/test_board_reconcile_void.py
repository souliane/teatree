import contextlib
from collections.abc import Generator
from unittest.mock import patch

from django.test import TestCase

from teatree.core.backend_protocols import PrMergeState, PrOpenState
from teatree.core.models import MergeAudit, MergeClear, PullRequest, Ticket
from teatree.core.models.known_issue import KnownIssue
from teatree.loop.scanners import board_reconcile
from teatree.loop.scanners.board_reconcile import reconcile_board
from tests.void_merge_fixtures import ISSUE_URL, MERGED_SHA, PR_URL, SLUG, landed_ticket, void_pr

State = Ticket.State


class _Overlay:
    def is_issue_done(self, issue_data: dict) -> bool:
        return issue_data.get("state") == "closed"


class _Host:
    def get_issue(self, issue_url: str) -> dict[str, object]:
        return {"state": "closed", "state_reason": "completed", "url": issue_url}


@contextlib.contextmanager
def _forge_after_the_void(probed: list[str]) -> Generator[None]:
    """The forge still reports the voided PR merged at the voided sha, and every issue closed."""

    def pr_state(url: str) -> PrOpenState:
        probed.append(url)
        return PrOpenState.MERGED

    with (
        patch.object(board_reconcile, "pr_open_state", pr_state),
        patch(
            "teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state",
            return_value=PrMergeState(state="MERGED", merge_commit_oid=MERGED_SHA),
        ),
        patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-teatree": _Overlay()}),
        patch("teatree.backends.issue_reads.get_code_host_for_url", return_value=_Host()),
    ):
        yield


class TestTheJanitorLeavesAVoidedTicketAlone(TestCase):
    def _reconcile(self) -> tuple[list[str], list[object]]:
        probed: list[str] = []
        with _forge_after_the_void(probed):
            report = reconcile_board()
        return probed, list(report.transitions)

    def test_its_own_voided_pr_url_is_neither_probed_nor_re_merged(self) -> None:
        landed_ticket()
        void_pr()
        keyed = Ticket.objects.create(overlay="t3-teatree", state=State.PR_OPENED, issue_url=PR_URL)

        probed, transitions = self._reconcile()

        keyed.refresh_from_db()
        assert keyed.state == State.PR_OPENED
        assert PR_URL not in probed
        assert transitions == []
        assert not KnownIssue.objects.filter(kind="board_merge_refused").exists()

    def test_a_done_issue_neither_delivers_nor_refuses_it(self) -> None:
        ticket = landed_ticket(from_state=State.REVIEW_REQUESTED)
        void_pr()

        _, transitions = self._reconcile()

        ticket.refresh_from_db()
        assert ticket.state == State.REVIEW_REQUESTED
        assert transitions == []

    def test_a_closed_issue_does_not_retire_it(self) -> None:
        ticket = landed_ticket()
        void_pr()

        _, transitions = self._reconcile()

        ticket.refresh_from_db()
        assert ticket.state == State.CODED
        assert transitions == []

    def test_an_older_abandoned_pr_neither_ends_the_hold_nor_refuses_each_run(self) -> None:
        ticket = landed_ticket(from_state=State.REVIEW_REQUESTED)
        PullRequest.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            url=f"https://github.com/{SLUG}/pull/40",
            repo=SLUG,
            iid="40",
            state=PullRequest.State.CLOSED,
        )
        void_pr()

        runs = [self._reconcile()[1] for _ in range(2)]

        ticket.refresh_from_db()
        assert ticket.state == State.REVIEW_REQUESTED
        assert runs == [[], []]


class TestTheHoldStaysOnTheTicketsTheVoidRestored(TestCase):
    def test_a_ticket_with_only_closed_prs_the_void_did_not_restore_is_still_retired(self) -> None:
        landed_ticket()
        void_pr()
        other = Ticket.objects.create(
            overlay="t3-teatree", state=State.CODED, issue_url=f"https://github.com/{SLUG}/issues/8"
        )
        PullRequest.objects.create(
            ticket=other,
            overlay="t3-teatree",
            url=f"https://github.com/{SLUG}/pull/40",
            repo=SLUG,
            iid="40",
            state=PullRequest.State.CLOSED,
        )

        with _forge_after_the_void([]):
            reconcile_board()

        other.refresh_from_db()
        assert other.state == State.IGNORED


class TestTheJanitorResumesOnceAnotherPrCarriesTheWork(TestCase):
    def test_a_new_pr_ends_the_hold(self) -> None:
        ticket = landed_ticket()
        void_pr()
        PullRequest.objects.create(
            ticket=ticket, overlay="t3-teatree", url=f"https://github.com/{SLUG}/pull/43", repo=SLUG, iid="43"
        )

        with _forge_after_the_void([]):
            reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == State.IGNORED

    def test_a_merged_re_delivery_lets_the_done_issue_walk_it_past_merged(self) -> None:
        ticket = landed_ticket(from_state=State.REVIEW_REQUESTED)
        void_pr()
        PullRequest.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            url=f"https://github.com/{SLUG}/pull/43",
            repo=SLUG,
            iid="43",
            state=PullRequest.State.MERGED,
        )

        with _forge_after_the_void([]):
            reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == State.RETRO_RECORDED

    def test_a_different_pr_merged_at_a_different_sha_advances_the_ticket(self) -> None:
        ticket = landed_ticket(issue_url=ISSUE_URL)
        void_pr()
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=42,
            slug=SLUG,
            reviewed_sha="8" * 40,
            reviewer_identity="cold-reviewer-42",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )
        MergeAudit.objects.create(clear=clear, merged_sha="9" * 40, required_checks_status="green", repo_slug=SLUG)
        PullRequest.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            url=f"https://github.com/{SLUG}/pull/42",
            repo=SLUG,
            iid="42",
            state=PullRequest.State.MERGED,
        )

        report = reconcile_board(probe_forge=False)

        ticket.refresh_from_db()
        assert ticket.state == State.MERGED
        assert [t.ticket_id for t in report.applied] == [ticket.pk]
