"""views._webhook_persistence (#116): provenance is stamped at the single ingestion chokepoint.

Every inbound flow funnels through ``persist_incoming_event``, so the trust provenance
is classified once here from ``(source, actor)`` — a trusted operator handle → ``owner``,
everyone else → the fail-closed ``public``.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import IncomingEvent, TrustedIdentity
from teatree.core.models.provenance import Provenance
from teatree.core.views import _webhook_persistence
from teatree.core.views._webhook_persistence import IngestionRecord, IngestOutcome, ingest, persist_incoming_event


@dataclass
class RecordingDispatch:
    handled: bool = True
    wake_error: Exception | None = None
    woken: list[int] = field(default_factory=list)

    def handles(self, record: IngestionRecord) -> bool:
        return self.handled

    def wake(self, event_pk: int) -> None:
        if self.wake_error is not None:
            raise self.wake_error
        self.woken.append(event_pk)


def dispatching(dispatch: RecordingDispatch):
    return patch.object(_webhook_persistence._INGEST, "dispatch", dispatch)


def _github_record(key: str = "github:h1", **fields: object) -> IngestionRecord:
    defaults: dict[str, object] = {
        "source": "github",
        "idempotency_key": key,
        "event_name": "pull_request_review",
        "action": "submitted",
        "origin": "webhook",
        "body": "approved",
        "payload_json": {"review": {"state": "approved"}},
        "settled": True,
    }
    return IngestionRecord(**(defaults | fields))


class TestProvenanceStamping(TestCase):
    def test_an_unknown_actor_is_stamped_public(self) -> None:
        assert persist_incoming_event(
            IngestionRecord(source="slack", idempotency_key="slack:p1", actor="stranger", body="hi")
        )
        event = IncomingEvent.objects.get(idempotency_key="slack:p1")
        assert event.provenance == Provenance.PUBLIC

    def test_a_trusted_operator_actor_is_stamped_owner(self) -> None:
        TrustedIdentity.objects.create(platform=TrustedIdentity.Platform.SLACK, handle="operator")
        assert persist_incoming_event(
            IngestionRecord(source="slack", idempotency_key="slack:p2", actor="operator", body="hi")
        )
        event = IncomingEvent.objects.get(idempotency_key="slack:p2")
        assert event.provenance == Provenance.OWNER

    def test_a_duplicate_insert_is_suppressed(self) -> None:
        record = IngestionRecord(source="slack", idempotency_key="slack:p3", actor="x", body="hi")
        assert persist_incoming_event(record) is True
        assert persist_incoming_event(record) is False


class TestDeadLetteringAtIngestion(TestCase):
    def test_a_record_with_a_dead_letter_reason_is_stored_already_dead_lettered(self) -> None:
        record = IngestionRecord(
            source="gitlab", idempotency_key="gitlab:pubsub:m1", dead_letter_reason="no webhook-id"
        )

        assert persist_incoming_event(record)

        event = IncomingEvent.objects.get(idempotency_key="gitlab:pubsub:m1")
        assert event.dead_lettered_at is not None
        assert event.last_error == "no webhook-id"
        assert event.processed_at is None
        assert not IncomingEvent.objects.unprocessed().exists()

    def test_a_record_without_a_reason_is_an_ordinary_unprocessed_event(self) -> None:
        persist_incoming_event(IngestionRecord(source="gitlab", idempotency_key="gitlab:ok", body="push"))

        event = IncomingEvent.objects.get(idempotency_key="gitlab:ok")
        assert event.dead_lettered_at is None
        assert event.last_error == ""
        assert IncomingEvent.objects.unprocessed().count() == 1


class TestSettledAtIngestion(TestCase):
    def test_pending_row_keeps_payload(self) -> None:
        ingest(
            IngestionRecord(source="gitlab", idempotency_key="gitlab:pending", body="retry", payload_json={"retry": 1})
        )

        row = IncomingEvent.objects.get(idempotency_key="gitlab:pending")
        assert (row.body, row.payload_json) == ("retry", {"retry": 1})

    def test_dead_lettered_row_keeps_payload_even_when_settled(self) -> None:
        ingest(
            IngestionRecord(
                source="gitlab",
                idempotency_key="gitlab:pubsub:poison",
                body="raw",
                payload_json={"data": "raw"},
                dead_letter_reason="message data is not JSON",
                settled=True,
            )
        )

        row = IncomingEvent.objects.get(idempotency_key="gitlab:pubsub:poison")
        assert (row.body, row.payload_json) == ("raw", {"data": "raw"})

    def test_ingest_duplicate_outcome(self) -> None:
        record = IngestionRecord(source="github", idempotency_key="github:once", settled=True)

        assert ingest(record) is IngestOutcome.NEW
        assert ingest(record) is IngestOutcome.DUPLICATE
        assert IncomingEvent.objects.count() == 1

    def test_a_settled_record_is_stored_processed_and_never_waits_for_a_drain(self) -> None:
        persist_incoming_event(IngestionRecord(source="gitlab", idempotency_key="gitlab:s1", settled=True))

        event = IncomingEvent.objects.get(idempotency_key="gitlab:s1")
        assert event.processed_at is not None
        assert event.dead_lettered_at is None
        assert not IncomingEvent.objects.unprocessed().exists()

    def test_a_record_not_marked_settled_is_left_unprocessed(self) -> None:
        persist_incoming_event(IngestionRecord(source="gitlab", idempotency_key="gitlab:s2"))

        assert IncomingEvent.objects.get(idempotency_key="gitlab:s2").processed_at is None


class TestHandledRecords(TestCase):
    def test_a_handled_record_is_stored_unsettled_with_its_payload_and_woken_after_commit(self) -> None:
        dispatch = RecordingDispatch()
        with dispatching(dispatch), self.captureOnCommitCallbacks() as callbacks:
            assert ingest(_github_record()) is IngestOutcome.NEW
            assert dispatch.woken == []

        row = IncomingEvent.objects.get()
        assert row.processed_at is None
        assert (row.body, row.payload_json) == ("approved", {"review": {"state": "approved"}})
        for callback in callbacks:
            callback()
        assert dispatch.woken == [row.pk]

    def test_a_dead_lettered_record_is_never_handled(self) -> None:
        dispatch = RecordingDispatch()
        with dispatching(dispatch), self.captureOnCommitCallbacks(execute=True):
            ingest(_github_record(dead_letter_reason="message data is not JSON"))

        row = IncomingEvent.objects.get()
        assert row.dead_lettered_at is not None
        assert dispatch.woken == []

    def test_a_failing_wake_still_stores_the_row_pending_and_says_so(self) -> None:
        dispatch = RecordingDispatch(wake_error=RuntimeError("queue down"))
        with (
            self.assertLogs(_webhook_persistence.logger, level=logging.WARNING) as logs,
            dispatching(dispatch),
            self.captureOnCommitCallbacks(execute=True),
        ):
            assert ingest(_github_record()) is IngestOutcome.NEW

        assert IncomingEvent.objects.get().processed_at is None
        assert any("not enqueued" in line for line in logs.output)

    def test_relay_and_poll_rows_are_not_subject_to_the_replay_window(self) -> None:
        dispatch = RecordingDispatch()
        stale = timezone.now() - timedelta(days=30)
        with dispatching(dispatch), self.captureOnCommitCallbacks(execute=True):
            for origin in ("relay", "poll"):
                ingest(_github_record(f"github:{origin}", origin=origin, occurred_at=stale))

        assert IncomingEvent.objects.filter(processed_at__isnull=True).count() == 2
        assert len(dispatch.woken) == 2
