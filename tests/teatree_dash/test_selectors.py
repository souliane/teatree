"""``build_kanban_columns`` groups tickets by FSM state with the right card badges (#3162)."""

from django.test import TestCase

from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.dash.selectors import BoardFilters, KanbanBoard, KanbanCard, build_kanban_columns
from tests.factories import PullRequestFactory, SessionFactory, TaskAttemptFactory, TaskFactory, TicketFactory

State = Ticket.State


def _cards_by_state(board: KanbanBoard) -> dict[str, list[KanbanCard]]:
    cards: dict[str, list[KanbanCard]] = {}
    for group in board.groups:
        for column in group.columns:
            cards[column.state] = list(column.cards)
    if board.ignored is not None:
        cards[board.ignored.state] = list(board.ignored.cards)
    return cards


def _find_card(board: KanbanBoard, ticket_id: int) -> KanbanCard | None:
    for cards in _cards_by_state(board).values():
        for card in cards:
            if card.ticket_id == ticket_id:
                return card
    return None


class BuildKanbanColumnsTestCase(TestCase):
    def test_tickets_land_in_their_state_column(self) -> None:
        started = TicketFactory(state=State.STARTED)
        merged = TicketFactory(state=State.MERGED)
        by_state = _cards_by_state(build_kanban_columns())
        assert [c.ticket_id for c in by_state[State.STARTED]] == [started.pk]
        assert [c.ticket_id for c in by_state[State.MERGED]] == [merged.pk]

    def test_ignored_hidden_by_default_and_shown_on_toggle(self) -> None:
        ignored = TicketFactory(state=State.IGNORED)
        assert _find_card(build_kanban_columns(), ignored.pk) is None
        shown = build_kanban_columns(BoardFilters(include_ignored=True))
        assert _find_card(shown, ignored.pk) is not None

    def test_active_dot_from_pending_task(self) -> None:
        ticket = TicketFactory(state=State.CODED)
        TaskFactory(ticket=ticket, session=SessionFactory(ticket=ticket), status="pending", phase="testing")
        card = _find_card(build_kanban_columns(), ticket.pk)
        assert card is not None
        assert card.active is True
        assert card.active_phase == "testing"

    def test_no_active_dot_without_open_work(self) -> None:
        ticket = TicketFactory(state=State.DELIVERED)
        card = _find_card(build_kanban_columns(), ticket.pk)
        assert card is not None
        assert card.active is False

    def test_last_error_from_task_attempt(self) -> None:
        ticket = TicketFactory(state=State.CODED)
        task = TaskFactory(ticket=ticket, session=SessionFactory(ticket=ticket))
        TaskAttemptFactory(task=task, error="boom: it failed")
        card = _find_card(build_kanban_columns(), ticket.pk)
        assert card is not None
        assert card.last_error == "boom: it failed"

    def test_dwell_from_latest_transition(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        TicketTransition.objects.create(
            ticket=ticket, from_state=State.SCOPED, to_state=State.STARTED, triggered_by="start"
        )
        card = _find_card(build_kanban_columns(), ticket.pk)
        assert card is not None
        assert card.dwell != ""

    def test_pr_chips(self) -> None:
        ticket = TicketFactory(state=State.SHIPPED)
        PullRequestFactory(ticket=ticket, repo="souliane/teatree", iid="42")
        card = _find_card(build_kanban_columns(), ticket.pk)
        assert card is not None
        assert len(card.pr_chips) == 1
        assert card.pr_chips[0].iid == "42"

    def test_overlay_and_kind_filters(self) -> None:
        keep = TicketFactory(state=State.STARTED, overlay="ovX", kind="fix")
        TicketFactory(state=State.STARTED, overlay="ovY", kind="feature")
        board = build_kanban_columns(BoardFilters(overlay="ovX", kind="fix"))
        ids = [c.ticket_id for cards in _cards_by_state(board).values() for c in cards]
        assert ids == [keep.pk]

    def test_text_filter_matches_description(self) -> None:
        keep = TicketFactory(state=State.STARTED, short_description="fix the widget resizer")
        TicketFactory(state=State.STARTED, short_description="unrelated")
        board = build_kanban_columns(BoardFilters(text="widget"))
        ids = [c.ticket_id for cards in _cards_by_state(board).values() for c in cards]
        assert ids == [keep.pk]
