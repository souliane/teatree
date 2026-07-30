"""Board cards and the ticket drawer link to the forge (#3624)."""

from django.test import TestCase
from django.urls import reverse

from teatree.core.models import PullRequest, Ticket
from teatree.dash.ticket_detail import build_ticket_detail

_ISSUE_URL = "https://github.com/souliane/teatree/issues/3624"


class TestCardForgeLinks(TestCase):
    def test_the_card_number_links_to_the_forge_issue(self) -> None:
        Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url=_ISSUE_URL, short_description="clickable cards")

        body = self.client.get(reverse("dash:board"), REMOTE_ADDR="127.0.0.1").content.decode()

        assert f'href="{_ISSUE_URL}"' in body

    def test_a_synthetic_loop_key_never_renders_a_dead_anchor(self) -> None:
        Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url="scanning-news://t3-teatree")

        body = self.client.get(reverse("dash:board"), REMOTE_ADDR="127.0.0.1").content.decode()

        assert "scanning-news://" not in body

    def test_a_pull_request_chip_links_to_the_forge(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url=_ISSUE_URL)
        PullRequest.objects.create(
            ticket=ticket,
            repo="souliane/teatree",
            iid=3624,
            url="https://github.com/souliane/teatree/pull/3624",
        )

        body = self.client.get(reverse("dash:board"), REMOTE_ADDR="127.0.0.1").content.decode()

        assert 'href="https://github.com/souliane/teatree/pull/3624"' in body


class TestDrawerForgeLink(TestCase):
    def test_the_detail_carries_the_derived_ref_not_a_bare_label(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url=_ISSUE_URL)

        detail = build_ticket_detail(ticket.pk)

        assert (detail.issue_href, detail.issue_ref) == (_ISSUE_URL, "#3624")

    def test_a_synthetic_key_derives_no_link(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url="scanning-news://t3-teatree")

        detail = build_ticket_detail(ticket.pk)

        assert (detail.issue_href, detail.issue_ref) == ("", "")

    def test_the_drawer_renders_the_ref_as_the_link_label(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url=_ISSUE_URL, short_description="cards")

        body = self.client.get(
            reverse("dash:ticket_drawer", args=[ticket.pk]), REMOTE_ADDR="127.0.0.1"
        ).content.decode()

        assert ">#3624<" in body
