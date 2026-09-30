"""``TicketEvidenceModel.rearm_review_at`` — a review re-aimed at a new head starts fresh (#959)."""

from django.test import TestCase

from teatree.core.modelkit.review_state import ReviewState
from teatree.core.models import Ticket

_REVIEWED_HEAD = "1f4b9c2ad0e7f61c83b25d90ac174e5f60a1b2c3"
_NEW_HEAD = "f89874729bb0a41ce6d5713a2c0e9f38b7a1d4e5"


def _reviewed_ticket() -> Ticket:
    return Ticket.objects.create(
        issue_url="https://github.com/o/r/pull/1",
        overlay="acme",
        role=Ticket.Role.REVIEWER,
        extra={
            "reviewed_sha": _REVIEWED_HEAD,
            "discharged_sha": _REVIEWED_HEAD,
            "last_review_state": ReviewState.APPROVED.value,
            "codex_variant": "codex:review",
        },
    )


class TestRearmReviewAt(TestCase):
    def test_a_new_head_drops_the_review_the_old_head_earned(self) -> None:
        ticket = _reviewed_ticket()

        ticket.rearm_review_at(_NEW_HEAD)

        ticket.refresh_from_db()
        assert ticket.extra == {"reviewed_sha": _NEW_HEAD, "codex_variant": "codex:review"}

    def test_the_reviewed_head_keeps_its_review(self) -> None:
        ticket = _reviewed_ticket()
        before = dict(ticket.extra)

        ticket.rearm_review_at(_REVIEWED_HEAD)

        ticket.refresh_from_db()
        assert ticket.extra == before

    def test_a_blank_head_changes_nothing(self) -> None:
        ticket = _reviewed_ticket()
        before = dict(ticket.extra)

        ticket.rearm_review_at("")

        ticket.refresh_from_db()
        assert ticket.extra == before
