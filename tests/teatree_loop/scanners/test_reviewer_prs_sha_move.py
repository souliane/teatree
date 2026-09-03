"""A SHA move on an approved colleague MR must still schedule a review (#959).

``ReviewerPrsScanner`` emits ``reviewer_pr.new_sha`` correctly when the head
moves, but on that same branch it advanced the cached ``reviewed_sha`` to the
NEW head while keeping the terminal ``last_review_state`` from the OLD one.
``persistence._handle_reviewer`` then saw ``reviewed_sha == head_sha``, skipped
its own ``last_review_state`` reset, and ``_already_reviewed_at_head``
suppressed the dispatch — no reviewing task, and the backup
``ReviewedPrHeadScanner`` silent too because the discharged SHA now equalled
the live head. The signal is the pre-fix code's correct half, so asserting on
the signal kind proves nothing: these drive scan -> dispatch ->
persist_agent_actions and assert on the Task.

The precondition is ordinary, not exotic: a GitLab MR-list payload carries no
``approvers``/``notes``, so an MR the user already approved still passes
``should_review_candidate_reasons``, and the scanner's own cache is what
recorded that approval.
"""

from dataclasses import dataclass, field

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.dispatch import dispatch
from teatree.loop.persistence import persist_agent_actions
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.types import RawAPIDict

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
MR_URL = "https://gitlab.example.com/team/project/-/merge_requests/6613"
REVIEWER = "reviewer-bot"


@dataclass
class FakeCodeHost:
    """In-memory ``CodeHostBackend`` covering the surface this scanner touches."""

    review_requested_prs: list[RawAPIDict] = field(default_factory=list)
    review_state: ReviewState = ReviewState.APPROVED

    def current_user(self) -> str:
        return REVIEWER

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return self.review_requested_prs

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = (pr_url, reviewer)
        return self.review_state

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        _ = pr_url
        return PrOpenState.OPEN


def _colleague_mr(head: str) -> RawAPIDict:
    """A GitLab MR-list payload: no ``approvers``, no ``notes`` — the live shape."""
    return {"web_url": MR_URL, "sha": head, "state": "opened", "author": {"username": "colleague"}}


def _scanner(head: str) -> ReviewerPrsScanner:
    return ReviewerPrsScanner(
        host=FakeCodeHost(review_requested_prs=[_colleague_mr(head)]),
        overlay_name="team-overlay",
    )


def _seed_approved_reviewer_ticket() -> Ticket:
    ticket = Ticket.objects.create(
        issue_url=MR_URL,
        overlay="team-overlay",
        role=Ticket.Role.REVIEWER,
        extra={"reviewed_sha": OLD_SHA, "last_review_state": ReviewState.APPROVED.value},
    )
    Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.REVIEW_POSTED)
    ticket.refresh_from_db()
    return ticket


class TestNewHeadOnAnApprovedMrSchedulesAReview(TestCase):
    def test_a_reviewing_task_is_created(self) -> None:
        _seed_approved_reviewer_ticket()

        created = persist_agent_actions(dispatch(_scanner(NEW_SHA).scan()))

        assert [task.phase for task in created] == ["reviewing"]
        assert Task.objects.filter(phase="reviewing", ticket__issue_url=MR_URL).count() == 1

    def test_the_stale_approval_is_dropped_from_the_reviewer_cache(self) -> None:
        """The recorded APPROVED belonged to the OLD head — carrying it forward is the defect."""
        ticket = _seed_approved_reviewer_ticket()

        _scanner(NEW_SHA).scan()

        ticket.refresh_from_db()
        assert (ticket.extra or {}).get("reviewed_sha") == NEW_SHA
        assert not (ticket.extra or {}).get("last_review_state")

    def test_an_unchanged_head_at_a_terminal_state_schedules_nothing(self) -> None:
        """Anti-vacuous control: the at-head dedup must survive the fix."""
        _seed_approved_reviewer_ticket()

        created = persist_agent_actions(dispatch(_scanner(OLD_SHA).scan()))

        assert created == []
        assert Task.objects.filter(phase="reviewing").count() == 0
