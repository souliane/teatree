"""Atomic ingest of normalized inbound records with duplicate suppression."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from django.db import IntegrityError, transaction
from django.utils import timezone

from teatree.core.models import IncomingEvent
from teatree.core.models.provenance import classify_provenance

logger = logging.getLogger(__name__)


class IngestOutcome(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"


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
    signer: str = ""
    origin: str = ""
    repo_key: str = ""
    action: str = ""
    occurred_at: datetime | None = None


def ingest(record: IngestionRecord) -> IngestOutcome:
    # The single ingestion chokepoint every inbound flow passes through (both webhook
    # views + the GitLab Pub/Sub scanner), so the #116 provenance is stamped ONCE
    # here from (source, actor) — the views need no change.
    provenance = classify_provenance(record.source, record.actor)
    moment = timezone.now()
    trim = (
        record.settled
        and not record.dead_letter_reason
        and record.source in {IncomingEvent.Source.GITHUB, IncomingEvent.Source.GITLAB}
    )
    try:
        with transaction.atomic():
            IncomingEvent.objects.create(
                source=record.source,
                signer=record.signer,
                origin=record.origin,
                repo_key=record.repo_key,
                action=record.action,
                occurred_at=record.occurred_at or moment,
                occurred_at_unknown=record.occurred_at is None,
                received_at=moment,
                actor=record.actor,
                channel_ref=record.channel_ref,
                thread_ref=record.thread_ref,
                body="" if trim else record.body,
                payload_json={} if trim else record.payload_json or {},
                idempotency_key=record.idempotency_key,
                event_name=record.event_name,
                provenance=provenance,
                last_error=record.dead_letter_reason,
                dead_lettered_at=moment if record.dead_letter_reason else None,
                processed_at=moment if record.settled else None,
            )
    except IntegrityError:
        logger.debug("%s already ingested — replay suppressed", record.idempotency_key)
        return IngestOutcome.DUPLICATE
    return IngestOutcome.NEW


def persist_incoming_event(record: IngestionRecord) -> bool:
    return ingest(record) is IngestOutcome.NEW
