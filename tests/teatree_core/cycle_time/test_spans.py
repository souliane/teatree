"""``spans_since`` is the one place a window read can be narrowed to one overlay (#4480).

Every aggregate over spans reaches the transition table through this function, so the
overlay filter belongs here and nowhere else — a second scope predicate downstream is a
second answer to one question. Scoping defaults OFF so the global dashboard read is
unchanged, and the boundary rule the whale tail depends on (a span is placed by where it
ENDED) has to survive the extra join.
"""

from datetime import timedelta

from django.test import TestCase

from teatree.core.cycle_time import spans_since
from teatree.core.models.ticket import Ticket
from tests.teatree_core.cycle_time.test_timeline import MINUTE, ORIGIN, at, record_transition

State = Ticket.State

WINDOW = ORIGIN - timedelta(days=1)


def ticket_in(overlay: str, *, minutes: float) -> Ticket:
    """A ticket in *overlay* whose only measurable span is ``planned -> coded``."""
    ticket = Ticket.objects.create(state=State.CODED, overlay=overlay)
    record_transition(ticket, source=State.NOT_STARTED, target=State.PLANNED, minutes=0)
    record_transition(ticket, source=State.PLANNED, target=State.CODED, minutes=minutes)
    return ticket


class OverlayScopeTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.mine = ticket_in("t3-teatree", minutes=10)
        cls.theirs = ticket_in("other-overlay", minutes=90)

    def test_an_unscoped_read_spans_every_overlay(self) -> None:
        assert {span.ticket_id for span in spans_since(WINDOW)} == {self.mine.pk, self.theirs.pk}

    def test_a_scoped_read_excludes_another_overlays_ticket(self) -> None:
        assert {span.ticket_id for span in spans_since(WINDOW, overlay="t3-teatree")} == {self.mine.pk}

    def test_an_overlay_with_no_tickets_reads_empty_rather_than_global(self) -> None:
        assert spans_since(WINDOW, overlay="no-such-overlay") == ()

    def test_the_scoped_read_keeps_the_measured_duration(self) -> None:
        assert [span.seconds for span in spans_since(WINDOW, overlay="t3-teatree")] == [10 * MINUTE]


class ScopedWindowBoundsTestCase(TestCase):
    """The scope filter must not disturb where a span is placed — by its END, not its start."""

    def test_a_long_span_that_ended_inside_the_window_survives_the_scope_filter(self) -> None:
        ticket_in("t3-teatree", minutes=120)
        assert [span.seconds for span in spans_since(at(60), overlay="t3-teatree")] == [120 * MINUTE]

    def test_a_span_that_ended_before_the_window_stays_excluded_under_scope(self) -> None:
        ticket_in("t3-teatree", minutes=10)
        assert spans_since(at(60), overlay="t3-teatree") == ()
