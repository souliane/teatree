"""Whether the pull request keeps its closing keyword: derived from what the rubric covers, never left at a default.

``more_prs_coming`` used to be read by ``should_close_ticket`` and written by nothing, so every pull
request defaulted to a closing keyword even on a first step. Only the forge boundary (the issue read)
is stubbed; the rubric, the waiver and the ticket row are real.
"""

from django.test import TestCase

from teatree.core.gates.rubric_gate import more_prs_coming_for, record_more_prs_coming
from teatree.core.models import Rubric, Ticket
from teatree.core.models.rubric_waiver import waive_criterion
from teatree.core.runners.ship import should_close_ticket
from tests._issue_read import issue_read
from tests.factories import waive_rubric

_ISSUE_URL = "https://github.com/souliane/teatree/issues/873"
_FIRST = "The first thing works"
_SECOND = "The second thing works"
_ISSUE = {"state": "open", "body": f"## Acceptance\n- {_FIRST}\n- {_SECOND}\n"}
_UMBRELLA = {"state": "open", "body": "- [ ] #11\n- [ ] #12\n"}


def _ticket(*criteria: str, issue_url: str = _ISSUE_URL, extra: dict | None = None) -> Ticket:
    ticket = Ticket.objects.create(overlay="t3-teatree", issue_url=issue_url, extra=extra or {})
    if criteria:
        Rubric.populate(ticket, list(criteria))
    return ticket


class TestShouldCloseTicket(TestCase):
    """The auto-close disposition resolver (#873): close-on-merge unless the flag says more is coming."""

    def test_the_setting_decides_when_nothing_says_more_is_coming(self) -> None:
        assert should_close_ticket({}, setting_enabled=True) is True
        assert should_close_ticket(None, setting_enabled=True) is True
        assert should_close_ticket({"more_prs_coming": False}, setting_enabled=True) is True

    def test_more_prs_coming_keeps_the_issue_open(self) -> None:
        assert should_close_ticket({"more_prs_coming": True}, setting_enabled=True) is False

    def test_a_disabled_setting_never_closes(self) -> None:
        assert should_close_ticket({}, setting_enabled=False) is False
        assert should_close_ticket({"more_prs_coming": True}, setting_enabled=False) is False


class TestMorePrsComingIsDerivedFromTheRubric(TestCase):
    def test_an_issue_item_no_criterion_covers_means_more_prs_are_coming(self) -> None:
        ticket = _ticket(_FIRST)

        with issue_read(_ISSUE):
            assert more_prs_coming_for(ticket) is True

    def test_every_item_covered_by_a_criterion_or_an_owner_waiver_means_none_are(self) -> None:
        ticket = _ticket(_FIRST, f"{_SECOND}, and it is proven")
        waived = Rubric.objects.active_for_ticket(ticket).criteria.get(text=_FIRST)
        waive_criterion(ticket, text_hash=waived.text_hash, waived_by="owner", reason="staged")

        with issue_read(_ISSUE):
            assert more_prs_coming_for(ticket) is False

    def test_an_issue_the_forge_cannot_read_means_more_prs_are_coming(self) -> None:
        with issue_read(RuntimeError("forge down")):
            assert more_prs_coming_for(_ticket(_FIRST, _SECOND)) is True

    def test_a_ticket_with_no_forge_issue_has_nothing_to_come(self) -> None:
        with issue_read(_ISSUE) as host:
            assert more_prs_coming_for(_ticket(_FIRST, issue_url="")) is False

        host.get_issue.assert_not_called()

    def test_the_audited_plan_bypass_has_nothing_to_come(self) -> None:
        ticket = _ticket()
        waive_rubric(ticket)

        with issue_read(_ISSUE):
            assert more_prs_coming_for(ticket) is False

    def test_an_umbrella_issue_always_has_more_prs_coming(self) -> None:
        with issue_read(_UMBRELLA):
            assert more_prs_coming_for(_ticket(_FIRST)) is True

    def test_an_umbrella_issue_has_more_prs_coming_under_the_plan_bypass_too(self) -> None:
        ticket = _ticket()
        waive_rubric(ticket)

        with issue_read(_UMBRELLA):
            assert more_prs_coming_for(ticket) is True

    def test_a_hand_declared_flag_survives_a_rubric_that_covers_every_item(self) -> None:
        ticket = _ticket(_FIRST, _SECOND, extra={"more_prs_coming": True})

        with issue_read(_ISSUE):
            extra = record_more_prs_coming(ticket)

        ticket.refresh_from_db()
        assert extra["more_prs_coming"] is True
        assert ticket.extra["more_prs_coming"] is True

    def test_a_flag_the_derivation_raised_drops_once_the_rubric_covers_every_item(self) -> None:
        ticket = _ticket(_FIRST)
        with issue_read(_ISSUE):
            assert record_more_prs_coming(ticket)["more_prs_coming"] is True

        Rubric.populate(ticket, [_FIRST, _SECOND])
        with issue_read(_ISSUE):
            extra = record_more_prs_coming(ticket)

        ticket.refresh_from_db()
        assert extra["more_prs_coming"] is False
        assert ticket.extra["more_prs_coming"] is False

    def test_recording_persists_the_derived_flag_and_returns_the_extra(self) -> None:
        ticket = _ticket(_FIRST)

        with issue_read(_ISSUE):
            extra = record_more_prs_coming(ticket)

        ticket.refresh_from_db()
        assert extra["more_prs_coming"] is True
        assert ticket.extra["more_prs_coming"] is True
