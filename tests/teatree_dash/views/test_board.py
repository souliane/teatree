"""The board page + column poll partial render tickets grouped by FSM state (#3162)."""

from django.test import TestCase
from django.urls import reverse

from teatree.core.models.ticket import Ticket
from teatree.dash.selectors import BOARD_COLUMNS
from tests.factories import TicketFactory

State = Ticket.State


class BoardPageTestCase(TestCase):
    def test_board_renders_and_lists_tickets(self) -> None:
        ticket = TicketFactory(state=State.STARTED, short_description="a board ticket")
        resp = self.client.get(reverse("dash:board"))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "a board ticket" in body
        assert f'data-ticket="{ticket.pk}"' in body

    def test_columns_partial_renders_all_board_states(self) -> None:
        resp = self.client.get(reverse("dash:board_columns"))
        assert resp.status_code == 200
        body = resp.content.decode()
        for state in BOARD_COLUMNS:
            assert f'data-state="{state}"' in body

    def test_ignored_hidden_until_toggled(self) -> None:
        ignored = TicketFactory(state=State.IGNORED, short_description="abandoned one")
        default = self.client.get(reverse("dash:board_columns"))
        assert f'data-ticket="{ignored.pk}"' not in default.content.decode()
        toggled = self.client.get(reverse("dash:board_columns"), {"ignored": "1"})
        assert f'data-ticket="{ignored.pk}"' in toggled.content.decode()

    def test_overlay_filter(self) -> None:
        keep = TicketFactory(state=State.STARTED, overlay="ovA", short_description="keep me")
        TicketFactory(state=State.STARTED, overlay="ovB", short_description="drop me")
        resp = self.client.get(reverse("dash:board_columns"), {"overlay": "ovA"})
        body = resp.content.decode()
        assert f'data-ticket="{keep.pk}"' in body
        assert "drop me" not in body
