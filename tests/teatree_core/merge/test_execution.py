"""``record_merge_and_advance`` — the merge post hook (consume + supersede + advance).

F2.8: the §15 sibling-CLEAR supersede matches the slug case-INSENSITIVELY. A
forge slug is case-insensitive, so a sibling CLEAR recorded with a
differently-cased ``owner/Repo`` must be consumed alongside the primary — a
case-mismatched orphan must not survive to keep ratcheting the S4 hard-red gate.
"""

from django.test import TestCase

from teatree.core.merge import execution
from teatree.core.merge.execution import record_merge_and_advance
from teatree.core.models import MergeClear, Ticket

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
