"""``handle_incoming_event``: claim, preset gate, dispatch, persist, then settle or retry."""

from datetime import timedelta
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import IncomingEvent, Task, Ticket
from teatree.core.models.incoming_event import MAX_INGEST_ATTEMPTS
from teatree.loop import incoming_event_handling
from teatree.loop.incoming_event_handling import handle_incoming_event
from teatree.loop.scanners.event_handlers import HandlingOutcome
from teatree.loop.scanners.incoming_events import IncomingEventsScanner
from tests._incoming_event_handlers import (
    ISSUE_URL,
    loop_row,
    pending_review_event,
    preset_admitting,
    raises,
    registered,
)


@django.test.override_settings(USE_TZ=True)
class HandlingTestCase(django.test.TestCase):
    def setUp(self) -> None:
        loop_row()
        preset_admitting(admitted=True)
        self.event = pending_review_event()

    def reload(self) -> IncomingEvent:
        self.event.refresh_from_db()
        return self.event


class TestHandledEvent(HandlingTestCase):
    def test_success_creates_the_task_and_settles_the_row_identity_only(self) -> None:
        with registered():
            assert handle_incoming_event(self.event) is HandlingOutcome.PROCESSED

        row = self.reload()
        assert row.processed_at is not None
        assert (row.body, row.payload_json) == ("", {})
        assert Ticket.objects.get().issue_url == ISSUE_URL
        assert Task.objects.count() == 1

    def test_an_event_with_no_handler_is_left_untouched(self) -> None:
        assert handle_incoming_event(self.event) is HandlingOutcome.SKIPPED

        row = self.reload()
        assert (row.processed_at, row.attempts, row.next_retry_at) == (None, 0, None)


class TestFailures(HandlingTestCase):
    def assert_retry_scheduled(self) -> None:
        row = self.reload()
        assert row.processed_at is None
        assert row.attempts == 1
        assert row.next_retry_at is not None
        assert row.next_retry_at > timezone.now() - timedelta(seconds=1)
        assert row.payload_json == {"review": {"state": "approved"}}

    def test_a_handler_that_raises_leaves_the_row_pending_for_a_retry(self) -> None:
        with registered(raises):
            assert handle_incoming_event(self.event) is HandlingOutcome.RETRY

        self.assert_retry_scheduled()
        assert "stub failure" in self.reload().last_error

    def test_a_persistence_failure_leaves_the_row_pending_for_a_retry(self) -> None:
        with (
            registered(),
            patch.object(incoming_event_handling, "persist_agent_actions", side_effect=RuntimeError("db down")),
        ):
            assert handle_incoming_event(self.event) is HandlingOutcome.RETRY

        self.assert_retry_scheduled()
        assert "db down" in self.reload().last_error

    def test_a_dispatch_error_entry_leaves_the_row_pending_for_a_retry(self) -> None:
        def fail_in_dispatch(signals: list, *, errors: dict[str, str]) -> list:
            errors["dispatch:issue_intake.admitted"] = "RuntimeError: no route"
            return []

        with registered(), patch.object(incoming_event_handling, "dispatch", fail_in_dispatch):
            assert handle_incoming_event(self.event) is HandlingOutcome.RETRY

        self.assert_retry_scheduled()
        assert "no route" in self.reload().last_error
        assert not Task.objects.exists()

    def test_the_last_allowed_failure_dead_letters_the_row(self) -> None:
        IncomingEvent.objects.filter(pk=self.event.pk).update(attempts=MAX_INGEST_ATTEMPTS - 1)

        with registered(raises):
            assert handle_incoming_event(IncomingEvent.objects.get()) is HandlingOutcome.DEAD_LETTERED

        assert self.reload().dead_lettered_at is not None


class TestPresetGate(HandlingTestCase):
    def test_a_loop_the_preset_switches_off_leaves_the_row_pending_until_it_is_admitted(self) -> None:
        preset_admitting(admitted=False)
        with registered():
            assert handle_incoming_event(self.event) is HandlingOutcome.DEFERRED

            row = self.reload()
            assert (row.processed_at, row.attempts, row.next_retry_at) == (None, 0, None)
            assert not Task.objects.exists()

            preset_admitting(admitted=True)
            IncomingEventsScanner(runner=handle_incoming_event).scan()

        assert self.reload().processed_at is not None
        assert Task.objects.count() == 1

    def test_a_handler_naming_a_loop_with_no_row_is_deferred_not_run(self) -> None:
        with registered(loop="no-such-loop"):
            assert handle_incoming_event(self.event) is HandlingOutcome.DEFERRED

        assert not Task.objects.exists()


class TestOneRunnerPerRow(HandlingTestCase):
    def test_a_row_another_runner_holds_is_left_alone(self) -> None:
        assert IncomingEvent.objects.claim_for_handling(self.event.pk, timezone.now()) is True

        with registered():
            assert handle_incoming_event(self.event) is HandlingOutcome.SKIPPED

        assert not Task.objects.exists()

    def test_a_wake_and_a_scanner_tick_holding_the_same_row_make_one_task(self) -> None:
        woken, ticked = IncomingEvent.objects.get(), IncomingEvent.objects.get()

        with registered():
            outcomes = [handle_incoming_event(woken), handle_incoming_event(ticked)]

        assert outcomes == [HandlingOutcome.PROCESSED, HandlingOutcome.SKIPPED]
        assert Task.objects.count() == 1
