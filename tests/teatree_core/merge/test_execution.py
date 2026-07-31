"""``record_merge_and_advance`` — the merge post hook (consume + supersede + advance).

F2.8: the §15 sibling-CLEAR supersede matches the slug case-INSENSITIVELY. A
forge slug is case-insensitive, so a sibling CLEAR recorded with a
differently-cased ``owner/Repo`` must be consumed alongside the primary — a
case-mismatched orphan must not survive to keep ratcheting the S4 hard-red gate.
"""

from django.test import TestCase

from teatree.core.merge import execution
from teatree.core.merge.post_hook import record_merge_and_advance
from teatree.core.models import MergeClear, PullRequest, Ticket

_SHA = "a" * 40


def test_execution_does_not_reexport_is_transient_merge_response() -> None:
    # F2.6: the merge_response docstring no longer claims execution re-exports the
    # transient classifier — it does NOT resolve as an execution attribute.
    assert not hasattr(execution, "_is_transient_merge_response")


def _clear(ticket: Ticket, *, slug: str, pr_id: int, reviewed_sha: str = _SHA) -> MergeClear:
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=pr_id,
        slug=slug,
        reviewed_sha=reviewed_sha,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )


def _pull_request(ticket: Ticket, *, slug: str, pr_id: int) -> PullRequest:
    return PullRequest.objects.create(
        ticket=ticket,
        overlay=ticket.overlay,
        url=f"https://github.com/{slug}/pull/{pr_id}",
        repo=slug,
        iid=str(pr_id),
    )


class TestKeystoneRecordsTheForgeMerge(TestCase):
    def test_merge_marks_the_pull_request_row_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        pr = _pull_request(ticket, slug="acme/widget", pr_id=42)

        state = record_merge_and_advance(
            clear=_clear(ticket, slug="acme/widget", pr_id=42),
            merged_sha="c" * 40,
            required_checks_status="green",
        )

        pr.refresh_from_db()
        ticket.refresh_from_db()
        assert pr.state == PullRequest.State.MERGED
        assert ticket.state == Ticket.State.MERGED
        assert state == Ticket.State.MERGED

    def test_ticketless_clear_adopts_the_pull_request_owning_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        pr = _pull_request(ticket, slug="acme/widget", pr_id=43)
        clear = MergeClear.objects.create(
            pr_id=43,
            slug="acme/widget",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

        state = record_merge_and_advance(clear=clear, merged_sha="c" * 40, required_checks_status="green")

        clear.refresh_from_db()
        pr.refresh_from_db()
        ticket.refresh_from_db()
        assert clear.ticket_id == ticket.pk
        assert pr.state == PullRequest.State.MERGED
        assert ticket.state == Ticket.State.MERGED
        assert state == Ticket.State.MERGED

    def test_unresolvable_ticketless_clear_still_merges_and_returns_no_state(self) -> None:
        clear = MergeClear.objects.create(
            pr_id=44,
            slug="acme/widget",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

        state = record_merge_and_advance(clear=clear, merged_sha="c" * 40, required_checks_status="green")

        clear.refresh_from_db()
        assert clear.ticket_id is None
        assert clear.consumed_at is not None
        assert state == ""

    def test_replayed_merge_leaves_an_already_merged_pull_request_row_alone(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        pr = _pull_request(ticket, slug="acme/widget", pr_id=45)
        pr.mark_merged()
        pr.save()

        record_merge_and_advance(
            clear=_clear(ticket, slug="acme/widget", pr_id=45),
            merged_sha="c" * 40,
            required_checks_status="green",
        )

        pr.refresh_from_db()
        assert pr.state == PullRequest.State.MERGED

    def test_a_differently_cased_clear_slug_still_advances_the_board(self) -> None:
        """A mis-cased CLEAR must not silently reinstate the starvation it fixes.

        Forge slugs are case-insensitive, so a CLEAR issued as ``acme/Widget``
        names the same PR as an ``acme/widget`` row. A case-sensitive lookup
        marks 0 rows and adopts no ticket, so the keystone returns nothing and
        the real merge moves no card.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        pr = _pull_request(ticket, slug="acme/widget", pr_id=46)
        clear = MergeClear.objects.create(
            pr_id=46,
            slug="Acme/Widget",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

        state = record_merge_and_advance(clear=clear, merged_sha="c" * 40, required_checks_status="green")

        clear.refresh_from_db()
        pr.refresh_from_db()
        ticket.refresh_from_db()
        assert clear.ticket_id == ticket.pk
        assert pr.state == PullRequest.State.MERGED
        assert ticket.state == Ticket.State.MERGED
        assert state == Ticket.State.MERGED


class TestSiblingSupersedeCaseInsensitive(TestCase):
    def test_differently_cased_sibling_clear_is_superseded(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        primary = _clear(ticket, slug="acme/Widget", pr_id=42)
        sibling = _clear(ticket, slug="acme/widget", pr_id=42, reviewed_sha="b" * 40)

        record_merge_and_advance(clear=primary, merged_sha="c" * 40, required_checks_status="green")

        primary.refresh_from_db()
        sibling.refresh_from_db()
        assert primary.consumed_at is not None
        # The case-mismatched sibling for the SAME PR is superseded (consumed),
        # so it can no longer keep ratcheting the S4 hard-red gate.
        assert sibling.consumed_at is not None

    def test_same_case_different_pr_is_not_superseded(self) -> None:
        # The supersede is scoped to the SAME PR — a different PR number (even a
        # case-matching slug) must be left untouched.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        primary = _clear(ticket, slug="acme/widget", pr_id=42)
        other_pr = _clear(ticket, slug="acme/widget", pr_id=43, reviewed_sha="b" * 40)

        record_merge_and_advance(clear=primary, merged_sha="c" * 40, required_checks_status="green")

        other_pr.refresh_from_db()
        assert other_pr.consumed_at is None

    def test_different_slug_same_pr_is_not_superseded(self) -> None:
        # A genuinely different repo slug (not merely a case variant) sharing the PR
        # number is a distinct PR and must not be superseded.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        primary = _clear(ticket, slug="acme/widget", pr_id=42)
        unrelated = _clear(ticket, slug="acme/gadget", pr_id=42, reviewed_sha="b" * 40)

        record_merge_and_advance(clear=primary, merged_sha="c" * 40, required_checks_status="green")

        unrelated.refresh_from_db()
        assert unrelated.consumed_at is None


class TestKeystoneReportsAnUnresolvableTicket(TestCase):
    """A merge whose ticket cannot be resolved must not pass silently (#3840).

    The post hook returned ``""`` and logged nothing when neither the CLEAR nor the
    PR named a ticket, so a merge landed, consumed its CLEAR and wrote its audit
    while the board stayed exactly where it was — with no record anywhere that an
    FSM advance had been skipped.
    """

    def test_warns_when_no_ticket_resolves(self) -> None:
        clear = MergeClear.objects.create(
            pr_id=42,
            slug="acme/widget",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

        with self.assertLogs("teatree.core.merge.post_hook", level="WARNING") as captured:
            state = record_merge_and_advance(clear=clear, merged_sha="c" * 40, required_checks_status="green")

        assert state == ""
        assert any("acme/widget#42" in line for line in captured.output), captured.output

    def test_a_resolvable_ticket_logs_no_warning(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pull_request(ticket, slug="acme/widget", pr_id=42)
        clear = _clear(ticket, slug="acme/widget", pr_id=42)

        with self.assertNoLogs("teatree.core.merge.post_hook", level="WARNING"):
            state = record_merge_and_advance(clear=clear, merged_sha="c" * 40, required_checks_status="green")

        assert state == Ticket.State.MERGED
