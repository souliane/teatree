"""The board page + column poll partial render tickets grouped by FSM state (#3162)."""

import os
import re
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from teatree.core.models.review_request_post import ReviewRequestPost
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.pr_inventory import PrInventoryEntry, write_inventory
from teatree.dash.selectors import BOARD_COLUMNS
from tests.factories import PullRequestFactory, TaskAttemptFactory, TaskFactory, TicketFactory

State = Ticket.State

_NON_LOOPBACK = "203.0.113.7"

_HX_GET_RE = re.compile(r'id="board"[^>]*\shx-get="([^"]*)"', re.DOTALL)


class PrBoardAcceptanceTestCase(TestCase):
    def test_default_board_shows_all_twenty_five_open_prs_with_action_groups(self) -> None:
        for number in range(25):
            ticket = TicketFactory(state=State.PR_OPENED, short_description=f"PR card {number}")
            PullRequestFactory(ticket=ticket)

        response = self.client.get(reverse("dash:board"))
        body = response.content.decode()
        assert response.status_code == 200
        assert body.count("data-pr-card=") == 25
        for label in ("Needs you", "Waiting on colleagues", "Factory working", "Ready to merge", "Blocked"):
            assert label in body
        assert "Lifecycle" in body

    def test_pr_card_keeps_the_same_dom_identity_across_poll(self) -> None:
        PullRequestFactory(ticket=TicketFactory())
        first = self.client.get(reverse("dash:board")).content.decode()
        polled = self.client.get(reverse("dash:board_prs")).content.decode()
        match = re.search(r'id="(pr-[0-9a-f]{16})"', first)
        assert match is not None
        assert match.group(0) in polled

    def test_pr_card_offers_only_fsm_eligible_actions(self) -> None:
        ticket = TicketFactory(state=State.TESTED)
        PullRequestFactory(ticket=ticket)
        body = self.client.get(reverse("dash:board")).content.decode()
        assert "Review now" in body
        assert "Ship now" not in body

        ticket.state = State.SELF_REVIEWED
        ticket.save(update_fields=["state"])
        body = self.client.get(reverse("dash:board")).content.decode()
        assert "Review now" not in body
        assert "Ship now" in body

    def test_pr_cards_expose_review_wait_and_active_factory_work(self) -> None:
        waiting_ticket = TicketFactory(state=State.REVIEW_REQUESTED)
        waiting = PullRequestFactory(ticket=waiting_ticket)
        waiting_ticket.extra = {
            "prs": {waiting.url: {"review_permalink": "https://app.slack.com/archives/C1/p1234567890000000"}}
        }
        waiting_ticket.save(update_fields=["extra"])
        ReviewRequestPost.objects.create(
            mr_url=waiting.url,
            slack_channel_id="C1",
            slack_thread_ts="1234567890.000000",
            nag_count=2,
            last_nag_at=timezone.now() - timedelta(days=1),
        )
        working_ticket = TicketFactory(state=State.PR_OPENED)
        PullRequestFactory(ticket=working_ticket)
        task = TaskFactory(
            ticket=working_ticket,
            phase="coding",
            status=Task.Status.CLAIMED,
            claimed_by="agent-1",
            claimed_at=timezone.now() - timedelta(hours=2),
        )
        TaskAttemptFactory(task=task, exit_code=0, result={"summary": "tests passed"})

        body = self.client.get(reverse("dash:board")).content.decode()
        assert "Review request" in body
        assert "Re-asks: 2" in body
        assert "Earliest re-ask" in body
        assert "agent-1" in body
        assert "tests passed" in body

    def test_unlinked_open_pr_from_tick_inventory_has_age_and_next_task(self) -> None:
        entry = PrInventoryEntry(
            url="https://github.com/souliane/teatree/pull/5007",
            repo="souliane/teatree",
            iid=5007,
            title="Unlinked pull request",
            created_at="2026-10-01T10:00:00Z",
            updated_at="2026-10-22T10:00:00Z",
            next_task="Run independent review",
            decision="flag_no_review",
            ci="unknown",
            approvals="unknown",
            threads="unknown",
            draft=False,
        )
        with TemporaryDirectory() as root, patch.dict(os.environ, {"T3_DATA_DIR": root}):
            write_inventory("teatree", [entry])
            body = self.client.get(reverse("dash:board")).content.decode()
        assert "Unlinked pull request" in body
        assert "Run independent review" in body
        assert "unknown" in body
        assert "age" in body.lower()

    def test_pr_card_refuses_unsafe_external_links(self) -> None:
        url = "javascript:alert(1)"
        ticket = TicketFactory(extra={"prs": {url: {"state": "opened", "review_permalink": url}}})
        PullRequestFactory(ticket=ticket, url=url)

        body = self.client.get(reverse("dash:board")).content.decode()
        assert 'href="javascript:' not in body
        assert "Review request" not in body
        assert "data-pr-card=" in body

    def test_lifecycle_backlog_and_delivered_page_independently_at_fifty(self) -> None:
        backlog = [TicketFactory(state=State.NOT_STARTED) for _ in range(55)]
        delivered = [TicketFactory(state=State.DELIVERED) for _ in range(55)]

        first = self.client.get(reverse("dash:board_lifecycle")).content.decode()
        assert first.count("data-ticket=") == 100
        assert f'data-ticket="{backlog[-1].pk}"' in first
        assert f'data-ticket="{backlog[0].pk}"' not in first
        assert f'data-ticket="{delivered[0].pk}"' not in first
        assert 'data-state="not_started"' in first
        assert 'data-state="delivered"' in first

        second = self.client.get(reverse("dash:board_lifecycle"), {"page_not_started": "1"}).content.decode()
        assert f'data-ticket="{backlog[0].pk}"' in second
        assert f'data-ticket="{delivered[-1].pk}"' in second
        assert f'data-ticket="{delivered[0].pk}"' not in second

    def test_lifecycle_is_a_separate_board_tab(self) -> None:
        ticket = TicketFactory(state=State.WORK_STARTED, short_description="lifecycle-only ticket")
        response = self.client.get(reverse("dash:board_lifecycle"))
        assert response.status_code == 200
        assert f'data-ticket="{ticket.pk}"' in response.content.decode()


class BoardPageTestCase(TestCase):
    def test_board_renders_and_lists_tickets(self) -> None:
        ticket = TicketFactory(state=State.WORK_STARTED, short_description="a board ticket")
        resp = self.client.get(reverse("dash:board_lifecycle"))
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
        keep = TicketFactory(state=State.WORK_STARTED, overlay="ovA", short_description="keep me")
        TicketFactory(state=State.WORK_STARTED, overlay="ovB", short_description="drop me")
        resp = self.client.get(reverse("dash:board_columns"), {"overlay": "ovA"})
        body = resp.content.decode()
        assert f'data-ticket="{keep.pk}"' in body
        assert "drop me" not in body


class BoardCardIssueLinkTestCase(TestCase):
    """The card's id IS the forge link (#3624); a sentinel renders plain text."""

    def test_forge_ticket_renders_the_number_as_a_clickable_anchor(self) -> None:
        ticket = TicketFactory(state=State.WORK_STARTED, issue_url="https://github.com/souliane/teatree/issues/3205")
        body = self.client.get(reverse("dash:board_columns")).content.decode()
        card = _card_fragment(body, ticket.pk)
        assert 'class="card-num mono"' in card
        assert 'href="https://github.com/souliane/teatree/issues/3205"' in card
        assert ">#3205</a>" in card

    def test_sentinel_ticket_renders_no_anchor(self) -> None:
        ticket = TicketFactory(state=State.NOT_STARTED, issue_url="scanning-news://t3-teatree")
        body = self.client.get(reverse("dash:board_columns")).content.decode()
        card = _card_fragment(body, ticket.pk)
        assert '<span class="card-num mono">' in card
        assert "<a " not in card


def _card_fragment(body: str, ticket_id: int) -> str:
    """The ``<article>…</article>`` markup for one ticket's card."""
    start = body.index(f'data-ticket="{ticket_id}"')
    open_tag = body.rindex("<article", 0, start)
    return body[open_tag : body.index("</article>", start)]


class BoardPollUrlEncodingTestCase(TestCase):
    """DASH-5: the htmx poll URL URL-encodes filter values.

    A search text containing ``&``/``=``/``+``/``#`` round-trips through the 4-second
    poll instead of silently splitting into extra params or truncating.
    """

    def _poll_filters(self, **params: str) -> dict[str, list[str]]:
        resp = self.client.get(reverse("dash:board_lifecycle"), params)
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

    def test_off_loopback_anonymous_pr_poll_and_lifecycle_are_refused(self) -> None:
        for route in ("dash:board_prs", "dash:board_lifecycle"):
            with self.subTest(route=route):
                response = self.client.get(reverse(route), REMOTE_ADDR=_NON_LOOPBACK)
                assert response.status_code == 403

    def test_loopback_board_page_still_serves(self) -> None:
        # 127.0.0.1 is the default test-client REMOTE_ADDR — the loopback bind the deploy relies on.
        assert self.client.get(reverse("dash:board")).status_code == 200

    def test_off_loopback_staff_user_passes_the_gate(self) -> None:
        staff = get_user_model().objects.create_user("boardstaff", password="x", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(reverse("dash:board"), REMOTE_ADDR=_NON_LOOPBACK)
        assert resp.status_code == 200


class CardEnqueueButtonsTestCase(TestCase):
    """#4085: prioritising a PR is a click on its card, not a raw DB query plus a CLI call."""

    def setUp(self) -> None:
        self.ticket = TicketFactory(state=State.WORK_STARTED)
        self.action = reverse("dash:task_action", args=[self.ticket.pk])

    def _card(self) -> str:
        return self.client.get(reverse("dash:board_columns")).content.decode()

    def test_the_card_hides_fsm_invalid_buttons(self) -> None:
        body = self._card()
        assert "Review now" not in body
        assert "Ship now" not in body

    def test_the_card_carries_valid_review_button(self) -> None:
        self.ticket.state = State.TESTED
        self.ticket.save(update_fields=["state"])
        body = self._card()
        assert f'hx-post="{self.action}"' in body
        assert "Review now" in body
        assert "Ship now" not in body

    def test_the_card_carries_valid_ship_button(self) -> None:
        self.ticket.state = State.SELF_REVIEWED
        self.ticket.save(update_fields=["state"])
        body = self._card()
        assert "Review now" not in body
        assert "Ship now" in body

    def test_the_button_does_not_also_open_the_drawer(self) -> None:
        self.ticket.state = State.TESTED
        self.ticket.save(update_fields=["state"])
        assert "event.stopPropagation()" in self._card()

    def test_a_queued_phase_renders_disabled_with_its_reason(self) -> None:
        self.ticket.state = State.TESTED
        self.ticket.save(update_fields=["state"])
        task = TaskFactory(ticket=self.ticket, phase="reviewing", status=Task.Status.PENDING)
        body = self._card()
        assert "disabled" in body
        assert f"TODO-{task.pk} is already queued for reviewing" in body

    def test_the_card_offers_no_phase_beyond_the_two(self) -> None:
        body = self._card()
        for absent in ("Scope now", "Code now", "Test now"):
            assert absent not in body, f"the card offers {absent!r} — that set belongs in the drawer"
