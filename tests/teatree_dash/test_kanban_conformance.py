"""The board columns must equal the ``Ticket.State`` FSM enum exactly (#3162).

The whole point of the kanban is that a ticket in any state is visible; a state
that silently dropped off the board would hide stuck work. This conformance test
fails RED the moment a new ``Ticket.State`` value is added without giving it a
column (or the toggle-hidden set).
"""

from teatree.core.models.ticket import Ticket
from teatree.dash.selectors import BOARD_COLUMNS, COLUMN_GROUPS, HIDDEN_STATES, all_column_states


def test_columns_equal_the_state_enum() -> None:
    covered = set(all_column_states())
    assert covered == set(Ticket.State.values)


def test_every_state_appears_exactly_once() -> None:
    columns = list(all_column_states())
    assert len(columns) == len(set(columns))
    assert len(columns) == len(Ticket.State.values)


def test_ignored_and_review_posted_are_the_hidden_states() -> None:
    assert HIDDEN_STATES == (Ticket.State.IGNORED, Ticket.State.REVIEW_POSTED)
    assert Ticket.State.IGNORED not in BOARD_COLUMNS
    assert Ticket.State.REVIEW_POSTED not in BOARD_COLUMNS


def test_board_columns_are_grouped_in_lifecycle_order() -> None:
    grouped = [state for _name, states in COLUMN_GROUPS for state in states]
    assert grouped == list(BOARD_COLUMNS)
    assert BOARD_COLUMNS[0] == Ticket.State.NOT_STARTED


def test_review_posted_is_hidden_not_in_any_column_group() -> None:
    """The reviewer terminal is hidden behind the toggle, not stacked in an active column."""
    grouped = {state for _name, states in COLUMN_GROUPS for state in states}
    assert Ticket.State.REVIEW_POSTED not in grouped
    assert Ticket.State.REVIEW_POSTED in HIDDEN_STATES
    # DELIVERED stays the author-merged terminal — a visible "Landed" column.
    assert Ticket.State.DELIVERED in dict(COLUMN_GROUPS)["Landed"]
