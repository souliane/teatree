"""``ticket_review_state.has_passed_review`` — post-review FSM classification (PR-08b)."""

from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.ticket_review_state import POST_REVIEW_STATES, has_passed_review

_POST_REVIEW = (
    Ticket.State.REVIEWED,
    Ticket.State.SHIPPED,
    Ticket.State.IN_REVIEW,
    Ticket.State.MERGED,
    Ticket.State.RETROSPECTED,
    Ticket.State.DELIVERED,
)
_PRE_REVIEW_OR_ABANDONED = (
    Ticket.State.NOT_STARTED,
    Ticket.State.SCOPED,
    Ticket.State.STARTED,
    Ticket.State.PLANNED,
    Ticket.State.CODED,
    Ticket.State.TESTED,
    # REVIEW_POSTED is a reviewer terminal, not the author "passed review"
    # milestone — a reviewer ticket is never a review-request candidate.
    Ticket.State.REVIEW_POSTED,
    Ticket.State.IGNORED,
)


class TestHasPassedReview(TestCase):
    def test_post_review_states_have_passed_review(self) -> None:
        for state in _POST_REVIEW:
            ticket = Ticket.objects.create(overlay="t3-teatree", state=state)
            assert has_passed_review(ticket), f"{state} should count as passed-review"

    def test_pre_review_and_abandoned_states_have_not_passed_review(self) -> None:
        for state in _PRE_REVIEW_OR_ABANDONED:
            ticket = Ticket.objects.create(overlay="t3-teatree", state=state)
            assert not has_passed_review(ticket), f"{state} should NOT count as passed-review"

    def test_post_review_set_is_a_complete_partition_of_all_states(self) -> None:
        """Structural guard: every ``State`` is classified post-review or not.

        ``POST_REVIEW_STATES`` is an explicit enumeration. Asserting the
        partition is exhaustive forces a FUTURE added State member to be
        classified consciously HERE, instead of silently defaulting to "not
        passed review" and re-blocking a canonically-progressed ticket.
        """
        all_states = set(Ticket.State)
        pre_or_abandoned = set(_PRE_REVIEW_OR_ABANDONED)
        assert POST_REVIEW_STATES.isdisjoint(pre_or_abandoned), "a state is both post-review and pre-review/abandoned"
        assert POST_REVIEW_STATES | pre_or_abandoned == all_states, (
            f"State partition incomplete — unclassified: {all_states - POST_REVIEW_STATES - pre_or_abandoned}"
        )
