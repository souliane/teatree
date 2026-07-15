"""Release-on-completion for issue-implementer markers.

A dispatched marker holds a slot in the single-ticket in-flight budget for
its whole lifetime. Before the release seam it was never cleared, so the
first claim locked the budget permanently. The ``post_transition`` receiver
here transitions every non-abandoned marker for the ticket's ``issue_url`` to
``COMPLETED`` the moment the ticket reaches a terminal state (MERGED /
DELIVERED / IGNORED), returning the slot to the budget.
"""

from django.test import TestCase

from teatree.core.models import ImplementedIssueMarker, Ticket
from tests.factories import ImplementedIssueMarkerFactory, TicketFactory

URL = "https://github.com/souliane/teatree/issues/3205"


class TestMarkerReleaseOnCompletion(TestCase):
    def _ticket_with_marker(self, *, state: Ticket.State, overlay: str = "t3-teatree"):
        ticket = TicketFactory(overlay=overlay, issue_url=URL, state=state)
        return ImplementedIssueMarkerFactory(overlay=overlay, issue_url=URL, ticket=ticket, ticket_created=True)

    def test_merge_releases_the_marker_and_frees_the_budget(self) -> None:
        """THE budget-releases-on-completion pin (the recorded #3205 stuck-marker bug)."""
        marker = self._ticket_with_marker(state=Ticket.State.IN_REVIEW)
        assert ImplementedIssueMarker.objects.in_flight_count("t3-teatree") == 1

        marker.ticket.reconcile_merged()
        marker.ticket.save()

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED
        assert ImplementedIssueMarker.objects.in_flight_count("t3-teatree") == 0

    def test_mark_merged_from_in_review_releases_the_marker(self) -> None:
        marker = self._ticket_with_marker(state=Ticket.State.IN_REVIEW)

        marker.ticket.mark_merged()
        marker.ticket.save()

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED

    def test_delivered_releases_the_marker(self) -> None:
        marker = self._ticket_with_marker(state=Ticket.State.RETROSPECTED)

        marker.ticket.mark_delivered()
        marker.ticket.save()

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED

    def test_ignored_releases_the_marker(self) -> None:
        marker = self._ticket_with_marker(state=Ticket.State.STARTED)

        marker.ticket.ignore()
        marker.ticket.save()

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED

    def test_in_progress_ticket_does_not_release_its_marker(self) -> None:
        """Regression: a still-in-flight ticket keeps its budget slot."""
        marker = self._ticket_with_marker(state=Ticket.State.CODED)

        marker.ticket.rework()
        marker.ticket.save()

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.TICKET_CREATED
        assert ImplementedIssueMarker.objects.in_flight_count("t3-teatree") == 1

    def test_abandoned_marker_is_not_resurrected_to_completed(self) -> None:
        """ABANDONED (give-up / fleet-steal) is terminal — completion must not overwrite it."""
        ticket = TicketFactory(overlay="t3-teatree", issue_url=URL, state=Ticket.State.IN_REVIEW)
        marker = ImplementedIssueMarkerFactory(overlay="t3-teatree", issue_url=URL, ticket=ticket, abandoned=True)

        ticket.reconcile_merged()
        ticket.save()

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.ABANDONED

    def test_only_matching_issue_url_markers_are_released(self) -> None:
        released = self._ticket_with_marker(state=Ticket.State.IN_REVIEW)
        other = ImplementedIssueMarkerFactory(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/9999",
            ticket_created=True,
        )

        released.ticket.reconcile_merged()
        released.ticket.save()

        other.refresh_from_db()
        assert other.state == ImplementedIssueMarker.State.TICKET_CREATED
