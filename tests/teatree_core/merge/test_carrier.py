"""The declared merge round: a carrier PR's ``Carries:`` line freezes its members."""

import pytest
from django.test import TestCase

from teatree.core.merge.carrier import frozen_by_carrier, parse_carries, record_carry_freeze
from teatree.core.models import PullRequest, Ticket
from teatree.core.models.errors import InvalidTransitionError
from tests.factories import planned_ticket

_CARRIER_URL = "https://github.com/acme/widget/pull/30"
_MEMBER_URLS = ["https://github.com/acme/widget/pull/31", "https://github.com/acme/widget/pull/32"]


class TestParseCarries:
    def test_reads_the_urls_of_the_single_carries_line(self) -> None:
        body = f"Merge round for the retro fixes.\n\nCarries: {_MEMBER_URLS[0]} {_MEMBER_URLS[1]}\n\nMore text."
        assert parse_carries(body) == tuple(_MEMBER_URLS)

    def test_no_line_declares_nothing(self) -> None:
        assert parse_carries("A PR body with no round declared.") == ()

    def test_two_lines_are_ambiguous_and_declare_nothing(self) -> None:
        body = f"Carries: {_MEMBER_URLS[0]}\nCarries: {_MEMBER_URLS[1]}"
        assert parse_carries(body) == ()

    def test_punctuation_after_a_url_is_not_part_of_it(self) -> None:
        assert parse_carries(f"Carries: {_MEMBER_URLS[0]}, {_MEMBER_URLS[1]}.") == tuple(_MEMBER_URLS)


class TestFrozenByCarrier(TestCase):
    @staticmethod
    def _pull_request(ticket: Ticket, url: str) -> PullRequest:
        return PullRequest.objects.create(
            ticket=ticket, overlay=ticket.overlay, url=url, repo="acme/widget", iid=url.rsplit("/", 1)[1]
        )

    def _round(self) -> tuple[Ticket, Ticket, PullRequest]:
        carrier = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/1")
        member = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/2")
        carrier_pr = self._pull_request(carrier, _CARRIER_URL)
        self._pull_request(member, _MEMBER_URLS[0])
        record_carry_freeze(carrier_ticket=carrier, carrier_url=_CARRIER_URL, member_urls=_MEMBER_URLS)
        member.refresh_from_db()
        carrier.refresh_from_db()
        return carrier, member, carrier_pr

    def test_recording_marks_the_member_and_the_carrier(self) -> None:
        carrier, member, _ = self._round()

        assert member.extra["carried_by"] == _CARRIER_URL
        assert carrier.extra["carries"] == _MEMBER_URLS

    def test_the_member_is_frozen_and_the_carrier_never_is(self) -> None:
        carrier, member, _ = self._round()

        assert frozen_by_carrier(member) == _CARRIER_URL
        assert frozen_by_carrier(carrier) is None

    def test_minting_a_coding_task_on_a_member_raises_naming_the_carrier(self) -> None:
        _, member, _ = self._round()

        with pytest.raises(InvalidTransitionError, match=_CARRIER_URL):
            member.schedule_coding()

    def test_minting_a_debugging_task_on_a_member_raises_naming_the_carrier(self) -> None:
        _, member, _ = self._round()

        with pytest.raises(InvalidTransitionError, match=_CARRIER_URL):
            member.schedule_implementing("debugging", reason="fix the red check")

    def test_the_carrier_ticket_still_mints_coding(self) -> None:
        carrier, _, _ = self._round()

        assert carrier.schedule_coding().phase == "coding"

    def test_a_carrier_that_is_itself_carried_is_never_frozen(self) -> None:
        outer = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/1")
        inner = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/2")
        deepest = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/3")
        for ticket, url in ((outer, _CARRIER_URL), (inner, _MEMBER_URLS[0]), (deepest, _MEMBER_URLS[1])):
            self._pull_request(ticket, url)

        record_carry_freeze(carrier_ticket=outer, carrier_url=_CARRIER_URL, member_urls=[_MEMBER_URLS[0]])
        record_carry_freeze(carrier_ticket=inner, carrier_url=_MEMBER_URLS[0], member_urls=[_MEMBER_URLS[1]])
        inner.refresh_from_db()

        assert inner.extra["carried_by"] == _CARRIER_URL
        assert frozen_by_carrier(inner) is None
        assert inner.schedule_coding().phase == "coding"

    def test_a_carrier_naming_its_own_pull_request_is_not_marked_as_carried(self) -> None:
        carrier = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/1")
        member = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/2")
        self._pull_request(carrier, _CARRIER_URL)
        self._pull_request(member, _MEMBER_URLS[0])

        record_carry_freeze(
            carrier_ticket=carrier, carrier_url=_CARRIER_URL, member_urls=[_CARRIER_URL, _MEMBER_URLS[0]]
        )
        carrier.refresh_from_db()
        member.refresh_from_db()

        assert "carried_by" not in carrier.extra
        assert member.extra["carried_by"] == _CARRIER_URL

    def _assert_released_by(self, released_state: str) -> None:
        _, member, carrier_pr = self._round()
        PullRequest.objects.filter(pk=carrier_pr.pk).update(state=released_state)

        assert frozen_by_carrier(member) is None
        assert member.schedule_coding().phase == "coding"

    def test_closing_the_carrier_releases_the_member(self) -> None:
        self._assert_released_by(PullRequest.State.CLOSED)

    def test_merging_the_carrier_releases_the_member(self) -> None:
        self._assert_released_by(PullRequest.State.MERGED)
