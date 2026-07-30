"""Tests for ``teatree.loop.manual_pr_reconcile`` — reconcile manual MRs into rows (#1912)."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models.pull_request import PullRequest
from teatree.core.models.ticket import Ticket
from teatree.loop.manual_pr_reconcile import reconcile_manual_prs
from teatree.loop.scanners.base import ScanSignal

_PR_URL = "https://github.com/souliane/teatree/pull/370"


def _signal(*, url: str = _PR_URL, iid: int = 370, description: str = "", raw_extra: dict | None = None) -> ScanSignal:
    raw = {"description": description, **(raw_extra or {})}
    return ScanSignal(
        kind="my_pr.open",
        summary=f"PR #{iid} open",
        payload={"url": url, "iid": iid, "title": "x", "status": "success", "raw": raw},
    )


class TestReconcileManualPrsFromFooter(TestCase):
    def _ticket(self, number: int = 855) -> Ticket:
        return Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=f"https://github.com/souliane/teatree/issues/{number}",
            state="started",
        )

    def test_manual_mr_with_closes_footer_gains_linked_row(self) -> None:
        ticket = self._ticket()

        created = reconcile_manual_prs([_signal(description="feat: does stuff\n\nCloses #855")])

        assert created == 1
        row = PullRequest.objects.get(url=_PR_URL)
        assert row.ticket_id == ticket.pk
        assert row.repo == "souliane/teatree"
        assert row.iid == "370"
        assert row.state == PullRequest.State.OPEN
        assert row.overlay == "t3-teatree"

    def test_reconciled_row_is_create_verification_confirmed(self) -> None:
        """#1194: a reconciled row is verify-by-re-read CONFIRMED — the scan is the re-read."""
        self._ticket()

        reconcile_manual_prs([_signal(description="Closes #855")])

        row = PullRequest.objects.get(url=_PR_URL)
        assert row.create_verification == PullRequest.CreateVerification.CONFIRMED
        assert row.create_verified_at is not None

    def test_re_tick_is_idempotent(self) -> None:
        self._ticket()
        signals = [_signal(description="Closes #855")]

        assert reconcile_manual_prs(signals) == 1
        assert reconcile_manual_prs(signals) == 0
        assert PullRequest.objects.filter(url=_PR_URL).count() == 1

    def test_footerless_mr_creates_no_row(self) -> None:
        self._ticket()

        created = reconcile_manual_prs([_signal(description="Related to #855, not closing")])

        assert created == 0
        assert not PullRequest.objects.filter(url=_PR_URL).exists()

    def test_footer_to_missing_ticket_creates_no_row(self) -> None:
        # A close footer whose target ticket does not exist stays statusline-only.
        created = reconcile_manual_prs([_signal(description="Closes #999")])

        assert created == 0
        assert not PullRequest.objects.filter(url=_PR_URL).exists()

    def test_footer_is_repo_namespaced_never_cross_repo(self) -> None:
        # Issue #855 exists on a DIFFERENT repo; the PR is on souliane/teatree.
        Ticket.objects.create(
            overlay="other",
            issue_url="https://github.com/other/project/issues/855",
            state="started",
        )
        created = reconcile_manual_prs([_signal(description="Closes #855")])

        assert created == 0, "a close footer must resolve against the PR's own repo, never a same-number foreign issue"


class TestReconcileManualPrsFromExtraPrs(TestCase):
    def test_extra_prs_fallback_resolves_footerless_mr(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/900",
            state="started",
            extra={"prs": {_PR_URL: {"iid": 370}}},
        )

        created = reconcile_manual_prs([_signal(description="no footer here")])

        assert created == 1
        assert PullRequest.objects.get(url=_PR_URL).ticket_id == ticket.pk


class TestReconcileManualPrsMerged(TestCase):
    def _ticket(self) -> Ticket:
        return Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/855",
            state="started",
        )

    def test_open_row_transitions_to_merged_on_live_merge(self) -> None:
        ticket = self._ticket()
        PullRequest.objects.create(ticket=ticket, overlay="t3-teatree", url=_PR_URL, repo="souliane/teatree", iid="370")

        changed = reconcile_manual_prs([_signal(description="Closes #855", raw_extra={"state": "merged"})])

        assert changed == 1
        assert PullRequest.objects.get(url=_PR_URL).state == PullRequest.State.MERGED

    def test_github_merged_flag_transitions_to_merged(self) -> None:
        self._ticket()

        reconcile_manual_prs([_signal(description="Closes #855", raw_extra={"merged": True})])

        assert PullRequest.objects.get(url=_PR_URL).state == PullRequest.State.MERGED

    def test_already_merged_row_stays_merged_idempotent(self) -> None:
        ticket = self._ticket()
        row = PullRequest.objects.create(
            ticket=ticket, overlay="t3-teatree", url=_PR_URL, repo="souliane/teatree", iid="370"
        )
        row.mark_merged()
        row.save()

        changed = reconcile_manual_prs([_signal(description="Closes #855", raw_extra={"state": "merged"})])

        assert changed == 0
        assert PullRequest.objects.get(url=_PR_URL).state == PullRequest.State.MERGED


class TestReconcileManualPrsSignalFilter(TestCase):
    def test_non_my_pr_signals_ignored(self) -> None:
        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/855",
            state="started",
        )
        other = ScanSignal(
            kind="reviewer_pr.new_sha",
            summary="not ours",
            payload={"url": _PR_URL, "iid": 370, "raw": {"description": "Closes #855"}},
        )

        assert reconcile_manual_prs([other]) == 0
        assert not PullRequest.objects.filter(url=_PR_URL).exists()

    def test_duplicate_urls_reconciled_once(self) -> None:
        self._ticket()
        sig = _signal(description="Closes #855")

        assert reconcile_manual_prs([sig, sig]) == 1
        assert PullRequest.objects.filter(url=_PR_URL).count() == 1

    def test_bad_row_is_isolated_and_others_still_reconcile(self) -> None:
        self._ticket()
        good = _signal(description="Closes #855")
        bad = _signal(url="https://github.com/souliane/teatree/pull/371", iid=371, description="Closes #855")

        def flaky(pr: object) -> bool:
            if pr.url.endswith("/371"):
                msg = "boom"
                raise RuntimeError(msg)
            return True

        with patch("teatree.loop.manual_pr_reconcile._reconcile_one", side_effect=flaky):
            count = reconcile_manual_prs([bad, good])

        assert count == 1

    def _ticket(self) -> Ticket:
        return Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/855",
            state="started",
        )
