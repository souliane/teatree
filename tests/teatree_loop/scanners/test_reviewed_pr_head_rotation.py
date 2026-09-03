"""Every watched reviewer ticket eventually gets its head checked.

``ReviewedPrHeadScanner`` caps its per-tick forge calls at ``max_checks``. The
cap was applied to a ``pk``-ordered list, so with more watched tickets than the
cap the same low-``pk`` window was re-checked every tick and everything past it
was never looked at again — the very "reviewed once, never again" failure the
scanner exists to close, one layer up.

The scanner drives the real ORM and the real ``Ticket.extra`` primitive; only
the forge is faked.
"""

from dataclasses import dataclass, field

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.reviewed_pr_head import ReviewedPrHeadScanner

OLD_SHA = "a" * 40
OVERLAY = "team-overlay"


@dataclass
class FakeCodeHost:
    """Reports every PR still at its reviewed SHA, and records what was asked."""

    checked: list[int] = field(default_factory=list)
    raise_for: frozenset[int] = frozenset()

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        _ = slug
        if pr_id in self.raise_for:
            msg = f"forge unavailable for !{pr_id}"
            raise RuntimeError(msg)
        self.checked.append(pr_id)
        return OLD_SHA

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        _ = pr_url
        return PrOpenState.OPEN

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = (pr_url, reviewer)
        return ReviewState.NONE


def _seed(pr_id: int) -> Ticket:
    ticket = Ticket.objects.create(
        issue_url=f"https://gitlab.example.com/team/project/-/merge_requests/{pr_id}",
        overlay=OVERLAY,
        role=Ticket.Role.REVIEWER,
        extra={"reviewed_sha": OLD_SHA, "last_review_state": ReviewState.APPROVED.value},
    )
    Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.REVIEW_POSTED)
    return ticket


class TestWatchSetRotation(TestCase):
    """A backlog larger than ``max_checks`` is covered across successive ticks."""

    def test_every_watched_ticket_is_checked_within_a_full_rotation(self) -> None:
        for pr_id in range(1, 7):
            _seed(pr_id)
        host = FakeCodeHost()
        scanner = ReviewedPrHeadScanner(host=host, overlay_name=OVERLAY, max_checks=2)

        for _tick in range(3):
            scanner.scan()

        assert sorted(host.checked) == [1, 2, 3, 4, 5, 6], (
            "a fixed pk-ordered slice re-checks the same head and starves the rest"
        )

    def test_a_ticket_whose_check_raises_does_not_re_claim_the_window(self) -> None:
        for pr_id in range(1, 4):
            _seed(pr_id)
        host = FakeCodeHost(raise_for=frozenset({1}))
        scanner = ReviewedPrHeadScanner(host=host, overlay_name=OVERLAY, max_checks=1)

        for _tick in range(3):
            scanner.scan()

        assert sorted(host.checked) == [2, 3]
