"""``ticket backfill-clears`` recovers the ticket link on ticketless CLEARs."""

from typing import cast

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.merge.clear_backfill import BackfillOutcome, ClearBackfillRow
from teatree.core.models import MergeAudit, MergeClear, PullRequest, Ticket

_SHA = "a" * 40


def _backfill(*args: str) -> list[ClearBackfillRow]:
    return cast("list[ClearBackfillRow]", call_command("ticket", "backfill-clears", *args))


def _ticketless_clear(*, slug: str = "acme/widget", pr_id: int = 42, consumed: bool = True) -> MergeClear:
    return MergeClear.objects.create(
        pr_id=pr_id,
        slug=slug,
        reviewed_sha=_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
        consumed_at=timezone.now() if consumed else None,
    )


def _merged_pr(*, slug: str = "acme/widget", pr_id: int = 42) -> PullRequest:
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
    return PullRequest.objects.create(
        ticket=ticket,
        overlay=ticket.overlay,
        url=f"https://github.com/{slug}/pull/{pr_id}",
        repo=slug,
        iid=str(pr_id),
    )


class TestBackfillClears(TestCase):
    def test_links_the_ticket_and_marks_the_pull_request_merged(self) -> None:
        pr = _merged_pr()
        clear = _ticketless_clear()

        rows = _backfill()

        clear.refresh_from_db()
        pr.refresh_from_db()
        assert clear.ticket_id == pr.ticket_id
        assert pr.state == PullRequest.State.MERGED
        assert [row.outcome for row in rows] == [BackfillOutcome.LINKED]

    def test_advances_the_ticket_when_the_clear_carries_its_merge_audit(self) -> None:
        pr = _merged_pr()
        clear = _ticketless_clear()
        MergeAudit.objects.create(clear=clear, merged_sha="c" * 40, required_checks_status="green")

        rows = _backfill()

        pr.ticket.refresh_from_db()
        assert pr.ticket.state == Ticket.State.MERGED
        assert rows[0].advanced_to == Ticket.State.MERGED

    def test_superseded_sibling_links_without_advancing_the_ticket(self) -> None:
        pr = _merged_pr()
        _ticketless_clear()

        rows = _backfill()

        pr.ticket.refresh_from_db()
        assert pr.ticket.state == Ticket.State.IN_REVIEW
        assert rows[0].advanced_to == ""
        assert "no merge audit" in rows[0].detail

    def test_a_manually_opened_pr_carried_only_in_ticket_extra_is_linked(self) -> None:
        """The UNRESOLVED detail claims this check ran — so it has to actually run.

        A CLEAR carries ``(slug, pr_id)`` and no PR url, so handing
        ``owning_ticket`` no url made the ``extra['prs']`` arm unreachable and
        reported every FK-less PR as having no ticket carrying it.
        """
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {"https://github.com/acme/widget/pull/42": {}}},
        )
        clear = _ticketless_clear()

        rows = _backfill()

        clear.refresh_from_db()
        assert clear.ticket_id == ticket.pk
        assert rows[0].outcome is BackfillOutcome.LINKED

    def test_a_differently_cased_clear_slug_resolves_its_pull_request(self) -> None:
        pr = _merged_pr()
        clear = _ticketless_clear(slug="Acme/Widget")

        rows = _backfill()

        clear.refresh_from_db()
        pr.refresh_from_db()
        assert clear.ticket_id == pr.ticket_id
        assert pr.state == PullRequest.State.MERGED
        assert rows[0].outcome is BackfillOutcome.LINKED

    def test_unresolvable_clear_is_reported_never_silently_skipped(self) -> None:
        clear = _ticketless_clear(pr_id=99)

        rows = _backfill()

        clear.refresh_from_db()
        assert clear.ticket_id is None
        assert rows[0].outcome is BackfillOutcome.UNRESOLVED
        assert "no PullRequest row" in rows[0].detail

    def test_live_unconsumed_clear_is_reported_and_left_alone(self) -> None:
        _merged_pr()
        clear = _ticketless_clear(consumed=False)

        rows = _backfill()

        clear.refresh_from_db()
        assert clear.ticket_id is None
        assert rows[0].outcome is BackfillOutcome.LIVE

    def test_dry_run_reports_without_persisting(self) -> None:
        pr = _merged_pr()
        clear = _ticketless_clear()

        rows = _backfill("--dry-run")

        clear.refresh_from_db()
        pr.refresh_from_db()
        assert clear.ticket_id is None
        assert pr.state == PullRequest.State.OPEN
        assert rows[0].outcome is BackfillOutcome.LINKED
        assert rows[0].ticket_id == pr.ticket_id

    def test_second_run_finds_nothing_left_to_link(self) -> None:
        _merged_pr()
        _ticketless_clear()

        _backfill()
        rows = _backfill()

        assert rows == []

    def test_a_clear_that_already_has_its_ticket_is_untouched(self) -> None:
        pr = _merged_pr()
        MergeClear.objects.create(
            ticket=pr.ticket,
            pr_id=42,
            slug="acme/widget",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

        assert _backfill() == []
