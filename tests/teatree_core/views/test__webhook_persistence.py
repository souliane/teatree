"""views._webhook_persistence (#116): provenance is stamped at the single ingestion chokepoint.

Every inbound flow funnels through ``persist_incoming_event``, so the trust provenance
is classified once here from ``(source, actor)`` — a trusted operator handle → ``owner``,
everyone else → the fail-closed ``public``.
"""

from django.test import TestCase

from teatree.core.models import IncomingEvent, TrustedIdentity
from teatree.core.models.provenance import Provenance
from teatree.core.views._webhook_persistence import IngestionRecord, persist_incoming_event


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
    def test_a_settled_record_is_stored_processed_and_never_waits_for_a_drain(self) -> None:
        persist_incoming_event(IngestionRecord(source="gitlab", idempotency_key="gitlab:s1", settled=True))

        event = IncomingEvent.objects.get(idempotency_key="gitlab:s1")
        assert event.processed_at is not None
        assert event.dead_lettered_at is None
        assert not IncomingEvent.objects.unprocessed().exists()

    def test_a_record_not_marked_settled_is_left_unprocessed(self) -> None:
        persist_incoming_event(IngestionRecord(source="gitlab", idempotency_key="gitlab:s2"))

        assert IncomingEvent.objects.get(idempotency_key="gitlab:s2").processed_at is None
