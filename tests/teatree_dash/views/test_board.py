"""The board page + column poll partial render tickets grouped by FSM state (#3162)."""

import re
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from teatree.core.models.ticket import Ticket
from teatree.dash.selectors import BOARD_COLUMNS
from tests.factories import TicketFactory

State = Ticket.State

_NON_LOOPBACK = "203.0.113.7"

_HX_GET_RE = re.compile(r'id="board"[^>]*\shx-get="([^"]*)"', re.DOTALL)


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


class BoardPollUrlEncodingTestCase(TestCase):
    """DASH-5: the htmx poll URL URL-encodes filter values.

    A search text containing ``&``/``=``/``+``/``#`` round-trips through the 4-second
    poll instead of silently splitting into extra params or truncating.
    """

    def _poll_filters(self, **params: str) -> dict[str, list[str]]:
        resp = self.client.get(reverse("dash:board"), params)
        assert resp.status_code == 200
        match = _HX_GET_RE.search(resp.content.decode())
        assert match is not None, "board poll (hx-get on #board) not found"
        return parse_qs(urlsplit(match.group(1)).query)

    def test_special_chars_round_trip_through_poll_url(self) -> None:
        # Without encoding, `&` starts a new param, `=` splits a key/value, `+`
        # decodes to a space, and `#` starts a fragment — all corrupting the filter.
        parsed = self._poll_filters(text="a&b=c+d#e", overlay="ov&x")
        assert parsed["text"] == ["a&b=c+d#e"]
        assert parsed["overlay"] == ["ov&x"]

    def test_plain_filters_still_carry_through(self) -> None:
        parsed = self._poll_filters(text="hello", role="author", kind="fix")
        assert parsed["text"] == ["hello"]
        assert parsed["role"] == ["author"]
        assert parsed["kind"] == ["fix"]


class BoardAccessGateTestCase(TestCase):
    """DASH-2: the board page + its poll partial carry the same loopback gate.

    An off-loopback anonymous GET is refused with 403, exactly like every other dash view.
    """

    def test_off_loopback_anonymous_board_page_is_refused(self) -> None:
        resp = self.client.get(reverse("dash:board"), REMOTE_ADDR=_NON_LOOPBACK)
        assert resp.status_code == 403

    def test_off_loopback_anonymous_columns_poll_is_refused(self) -> None:
        resp = self.client.get(reverse("dash:board_columns"), REMOTE_ADDR=_NON_LOOPBACK)
        assert resp.status_code == 403

    def test_loopback_board_page_still_serves(self) -> None:
        # 127.0.0.1 is the default test-client REMOTE_ADDR — the loopback bind the deploy relies on.
        assert self.client.get(reverse("dash:board")).status_code == 200

    def test_off_loopback_staff_user_passes_the_gate(self) -> None:
        staff = get_user_model().objects.create_user("boardstaff", password="x", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(reverse("dash:board"), REMOTE_ADDR=_NON_LOOPBACK)
        assert resp.status_code == 200
