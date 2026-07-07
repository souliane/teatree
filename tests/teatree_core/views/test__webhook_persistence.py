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
        assert event.is_untrusted is True

    def test_a_trusted_operator_actor_is_stamped_owner(self) -> None:
        TrustedIdentity.objects.create(platform=TrustedIdentity.Platform.SLACK, handle="operator")
        assert persist_incoming_event(
            IngestionRecord(source="slack", idempotency_key="slack:p2", actor="operator", body="hi")
        )
        event = IncomingEvent.objects.get(idempotency_key="slack:p2")
        assert event.provenance == Provenance.OWNER
        assert event.is_untrusted is False

    def test_a_duplicate_insert_is_suppressed(self) -> None:
        record = IngestionRecord(source="slack", idempotency_key="slack:p3", actor="x", body="hi")
        assert persist_incoming_event(record) is True
        assert persist_incoming_event(record) is False
