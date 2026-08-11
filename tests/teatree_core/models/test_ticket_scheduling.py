"""The phase-task scheduling seam is idempotent in its side effects (#3903).

``_schedule_phase_task`` is the one chokepoint every auto-FSM phase task is minted
through. It used to mint unconditionally, so a second call for a ticket+phase that
already had a live task produced a SECOND Session + Task — two agents claimed and
dispatched against one worktree. The guard now lives at the write rather than in
each of the N callers.

The two directions are pinned separately: an in-flight sibling is a DUPLICATE and
collapses; a terminal sibling is a finished attempt and the next call is legitimate
re-work that must still mint.
"""

from django.test import TestCase

from teatree.core.models import Session, Task, Ticket


class TestScheduleHeadlessCollapsesDuplicates(TestCase):
    """A second schedule for a ticket+phase that is already in flight returns the live task."""

    @staticmethod
    def _author() -> Ticket:
        return Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")

    def test_two_schedule_coding_calls_yield_one_task_and_one_session(self) -> None:
        ticket = self._author()

        first = ticket.schedule_coding()
        second = ticket.schedule_coding()

        assert second.pk == first.pk, "the second schedule minted a rival coding task for the same ticket"
        assert Task.objects.filter(ticket=ticket, phase="coding").count() == 1
        assert Session.objects.filter(ticket=ticket, agent_id="coding").count() == 1

    def test_a_claimed_sibling_is_a_duplicate_not_a_retry(self) -> None:
        # The observed incident: task 669 was CLAIMED and heartbeating when 670 was
        # minted. A live lease is the strongest possible "already being worked".
        ticket = self._author()
        first = ticket.schedule_coding()
        first.status = Task.Status.CLAIMED
        first.save(update_fields=["status"])

        second = ticket.schedule_coding()

        assert second.pk == first.pk
        assert Task.objects.filter(ticket=ticket, phase="coding").count() == 1

    def test_a_short_verb_row_dedupes_against_the_gerund_phase(self) -> None:
        # ``tasks create <id> review`` stores the short verb; the dedupe lock must
        # read it as the same phase or the seam mints a rival for every spelling.
        ticket = self._author()
        session = Session.objects.create(ticket=ticket, agent_id="review")
        stored = Task.objects.create(ticket=ticket, session=session, phase="review")

        scheduled = ticket.schedule_review()

        assert scheduled.pk == stored.pk
        assert Task.objects.filter(ticket=ticket).count() == 1

    def test_every_auto_fsm_phase_collapses(self) -> None:
        ticket = self._author()

        for schedule in (ticket.schedule_planning, ticket.schedule_coding, ticket.schedule_testing):
            assert schedule().pk == schedule().pk

        assert Task.objects.filter(ticket=ticket).count() == 3


class TestScheduleHeadlessStillMintsLegitimateRework(TestCase):
    """The guard is not over-eager: a finished attempt never blocks the next one."""

    @staticmethod
    def _author() -> Ticket:
        return Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2")

    def test_a_failed_attempt_is_followed_by_a_fresh_task(self) -> None:
        # A genuine second attempt after a real failure — the failure direction the
        # dedupe must never swallow.
        ticket = self._author()
        first = ticket.schedule_coding()
        first.status = Task.Status.FAILED
        first.save(update_fields=["status"])

        second = ticket.schedule_coding()

        assert second.pk != first.pk, "a failed attempt blocked its own retry"
        assert second.session_id != first.session_id, "re-work must run in a fresh session"

    def test_a_completed_attempt_is_followed_by_a_fresh_task(self) -> None:
        ticket = self._author()
        first = ticket.schedule_coding()
        first.status = Task.Status.COMPLETED
        first.save(update_fields=["status"])

        second = ticket.schedule_coding()

        assert second.pk != first.pk

    def test_distinct_phases_never_dedupe_against_each_other(self) -> None:
        ticket = self._author()

        planning = ticket.schedule_planning()
        coding = ticket.schedule_coding()

        assert planning.pk != coding.pk
        assert Task.objects.filter(ticket=ticket).count() == 2

    def test_a_sibling_ticket_in_the_same_phase_is_not_a_duplicate(self) -> None:
        # Overlay scoping is NOT asserted here: this seam narrows by ``ticket``, which
        # already implies the overlay, so an overlay case at this level passes with the
        # overlay predicate deleted. That axis belongs to — and is pinned at — the
        # manager (``test_in_flight_for_phase_scopes_to_overlay_and_phase``).
        mine = self._author()
        theirs = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3")

        assert mine.schedule_coding().pk != theirs.schedule_coding().pk
        assert Task.objects.filter(phase="coding").count() == 2
