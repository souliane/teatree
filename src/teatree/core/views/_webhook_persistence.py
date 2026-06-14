"""Shared persistence helper for inbound webhook receivers (#654).

Each platform view extracts a normalized record from its payload and calls
:func:`persist_incoming_event`. The helper wraps the
``IncomingEvent.objects.create()`` call in a nested ``transaction.atomic()``
so a duplicate insert under Django's test transaction doesn't poison the
outer block — it just lets the platform-specific replay path no-op.
"""

import logging
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction

from teatree.core.models import IncomingEvent

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestionRecord:
    source: str
    idempotency_key: str
    actor: str = ""
    channel_ref: str = ""
    thread_ref: str = ""
    parent_ts: str = ""
    parent_text: str = ""
    body: str = ""
    payload_json: dict = field(default_factory=dict)


def persist_incoming_event(record: IngestionRecord) -> bool:
    try:
        with transaction.atomic():
            IncomingEvent.objects.create(
                source=record.source,
                actor=record.actor,
                channel_ref=record.channel_ref,
                thread_ref=record.thread_ref,
                parent_ts=record.parent_ts,
                parent_text=record.parent_text,
                body=record.body,
                payload_json=record.payload_json or {},
                idempotency_key=record.idempotency_key,
            )
    except IntegrityError:
        logger.debug("%s already ingested — replay suppressed", record.idempotency_key)
        return False
    return True
