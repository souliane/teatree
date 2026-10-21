"""Shared persistence helper for inbound webhook receivers (#654).

Each platform view extracts a normalized record and calls :func:`ingest_response`.
A nested transaction lets duplicate deliveries no-op without poisoning a caller's transaction.
Only database lock errors become retryable HTTP responses.
"""

import logging
from dataclasses import dataclass, field

from django.db import IntegrityError, OperationalError, transaction
from django.http import HttpResponse
from django.utils import timezone

from teatree.core.modelkit.db_retry import is_locked_error
from teatree.core.models import IncomingEvent
from teatree.core.models.provenance import classify_provenance

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestionRecord:
    source: str
    idempotency_key: str
    actor: str = ""
    channel_ref: str = ""
    thread_ref: str = ""
    body: str = ""
    payload_json: dict = field(default_factory=dict)
    dead_letter_reason: str = ""
    settled: bool = False
    event_name: str = ""


def persist_incoming_event(record: IngestionRecord) -> bool:
    # The single ingestion chokepoint every inbound flow passes through (both webhook
    # views + the GitLab Pub/Sub scanner), so the #116 provenance is stamped ONCE
    # here from (source, actor) — the views need no change.
    provenance = classify_provenance(record.source, record.actor)
    try:
        with transaction.atomic():
            IncomingEvent.objects.create(
                source=record.source,
                actor=record.actor,
                channel_ref=record.channel_ref,
                thread_ref=record.thread_ref,
                body=record.body,
                payload_json=record.payload_json or {},
                idempotency_key=record.idempotency_key,
                event_name=record.event_name,
                provenance=provenance,
                last_error=record.dead_letter_reason,
                dead_lettered_at=timezone.now() if record.dead_letter_reason else None,
                processed_at=timezone.now() if record.settled else None,
            )
    except IntegrityError:
        logger.debug("%s already ingested — replay suppressed", record.idempotency_key)
        return False
    return True


def ingest_response(record: IngestionRecord) -> HttpResponse:
    try:
        persist_incoming_event(record)
    except OperationalError as exc:
        if not is_locked_error(exc):
            raise
        logger.warning("%s not stored: the database is locked, answered 503", record.idempotency_key)
        return HttpResponse(status=503)
    return HttpResponse(status=200)
