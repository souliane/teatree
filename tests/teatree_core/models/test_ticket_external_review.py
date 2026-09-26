"""The PR-state gate every reviewer mint passes through (#4847)."""

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.ticket_external_review import (
    ReviewDeclined,
    reviewer_dispatch_decline,
    schedule_external_review,
)
from tests._pr_open_state_stub import pr_open_state

_PR = "https://github.com/org/app/pull/7"
_UNKNOWN_STATE = "retired_state_zz"
_LOGGER = "teatree.core.models.ticket_external_review"


def _reviewer(**overrides: object) -> Ticket:
    fields: dict[str, object] = {"overlay": "acme", "issue_url": _PR, "role": Ticket.Role.REVIEWER}
    fields.update(overrides)
    return Ticket.objects.create(**fields)


class TestFinishedPrMintsNothing(TestCase):
    def test_a_merged_pr_mints_no_task_and_retires_the_ticket(self) -> None:
        ticket = _reviewer()

        with pr_open_state(PrOpenState.MERGED), self.assertLogs(_LOGGER, level="INFO") as logs:
            result = schedule_external_review(ticket)

        assert result is ReviewDeclined.RETIRED
        assert not Task.objects.filter(ticket=ticket).exists()
        assert not Session.objects.filter(ticket=ticket).exists()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
        assert any("Retired reviewer ticket" in line for line in logs.output)

    def test_a_closed_pr_mints_no_task_and_retires_the_ticket(self) -> None:
        ticket = _reviewer()

        with pr_open_state(PrOpenState.CLOSED):
            result = schedule_external_review(ticket)

        assert result is ReviewDeclined.RETIRED
        assert not Task.objects.filter(ticket=ticket).exists()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED

    def test_a_settled_pr_on_a_ticket_that_cannot_be_ignored_is_not_reported_retired(self) -> None:
        ticket = _reviewer(state=Ticket.State.REVIEW_DELIVERED)

        with pr_open_state(PrOpenState.MERGED), self.assertLogs(_LOGGER, level="INFO") as logs:
            result = schedule_external_review(ticket)

        assert result is ReviewDeclined.NOTHING_OWED
        assert not Task.objects.filter(ticket=ticket).exists()
        assert not Session.objects.filter(ticket=ticket).exists()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_DELIVERED
        assert not any("Retired reviewer ticket" in line for line in logs.output)
        assert sum("Not retiring" in line for line in logs.output) == 1


class TestUnreadablePrStateFailsClosed(TestCase):
    def _assert_declined_untouched(self, ticket: Ticket, result: object) -> None:
        assert result is ReviewDeclined.PR_STATE_UNKNOWN
        assert not Task.objects.filter(ticket=ticket).exists()
        assert not Session.objects.filter(ticket=ticket).exists()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.NOT_STARTED

    def test_an_unknown_forge_state_mints_nothing(self) -> None:
        ticket = _reviewer()

        with pr_open_state(PrOpenState.UNKNOWN):
            result = schedule_external_review(ticket)

        self._assert_declined_untouched(ticket, result)

    def test_a_forge_read_that_raises_mints_nothing(self) -> None:
        ticket = _reviewer()

        with pr_open_state(raises=True):
            result = schedule_external_review(ticket)

        self._assert_declined_untouched(ticket, result)

    def test_no_code_host_for_the_pr_mints_nothing(self) -> None:
        ticket = _reviewer()

        with pr_open_state(no_host=True):
            result = schedule_external_review(ticket)

        self._assert_declined_untouched(ticket, result)


class TestUnreviewableStateSkipsTheForge(TestCase):
    def test_an_ignored_ticket_is_owed_nothing_and_the_forge_is_not_read(self) -> None:
        ticket = _reviewer(state=Ticket.State.IGNORED)

        with pr_open_state() as host:
            result = schedule_external_review(ticket)

        assert result is ReviewDeclined.NOTHING_OWED
        assert host.calls == []
        assert not Task.objects.filter(ticket=ticket).exists()

    def test_an_unknown_state_is_owed_nothing_warns_and_the_forge_is_not_read(self) -> None:
        ticket = _reviewer()
        Ticket.objects.filter(pk=ticket.pk).update(state=_UNKNOWN_STATE)
        ticket.refresh_from_db()

        with pr_open_state() as host, self.assertLogs("teatree.core.models.ticket_external_review", "WARNING") as logs:
            result = schedule_external_review(ticket)

        assert result is ReviewDeclined.NOTHING_OWED
        assert host.calls == []
        assert any(_UNKNOWN_STATE in line for line in logs.output)
        assert not Task.objects.filter(ticket=ticket).exists()


class TestOpenPrStillGetsItsReview(TestCase):
    def test_an_open_pr_mints_the_reviewing_task(self) -> None:
        ticket = _reviewer()

        with pr_open_state(PrOpenState.OPEN) as host:
            result = schedule_external_review(ticket)

        assert isinstance(result, Task)
        assert result.phase == "reviewing"
        assert host.calls == [_PR]

    def test_an_in_flight_sibling_is_returned_without_a_forge_read(self) -> None:
        ticket = _reviewer()
        with pr_open_state():
            first = schedule_external_review(ticket)

        with pr_open_state(PrOpenState.MERGED) as host:
            second = schedule_external_review(ticket)

        assert isinstance(first, Task)
        assert second == first
        assert host.calls == []

    def test_the_decline_gate_admits_an_open_pr(self) -> None:
        with pr_open_state():
            assert reviewer_dispatch_decline(_reviewer()) is None


def test_sweep_admission_never_admits_a_state_the_review_cannot_advance() -> None:
    field = Ticket._meta.get_field("state")
    sources = {
        transition.source
        for transition in field.get_all_transitions(Ticket)
        if transition.name == "mark_reviewed_externally"
    }

    assert Ticket.pre_ship_states() <= sources
