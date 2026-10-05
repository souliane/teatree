"""Scanner that pulls GitLab group-webhook events from a Pub/Sub subscription into ``IncomingEvent``.

The relay publishes each verified delivery as one message: the raw GitLab body as data, with
``webhook-id`` and ``X-Gitlab-Event`` attributes, and ``body-omitted`` when the body was too
large to carry. A message is acknowledged only after its row is stored, so a crash or a store
failure redelivers it and the ``gitlab:<webhook-id>`` key drops the repeat.

Every row is stored already processed: ``IncomingEventsScanner`` would route a group ``approved``
event to the overlay merge guard with a project path instead of an MR URL, and read the body as
free text, so nothing drains these rows until a GitLab-aware route exists.
"""

import json
import logging
from dataclasses import dataclass

from django.db import DatabaseError

from teatree.backends.pubsub import PubsubMessage, PubsubSubscription
from teatree.core.models import IncomingEvent
from teatree.core.views._webhook_persistence import IngestionRecord, persist_incoming_event
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, ScanSignal
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

PULL_BATCH = 50
MAX_PULLS_PER_TICK = 5
DEAD_LETTER_DATA_BYTES = 2048


def _text(payload: RawAPIDict, *path: str) -> str:
    value: object = payload
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return str(value or "")


def _record_for(message: PubsubMessage) -> IngestionRecord:
    webhook_id = message.attributes.get("webhook-id", "")
    if not webhook_id:
        return _dead_letter(message, "message without webhook-id")
    key = f"gitlab:{webhook_id}"
    if message.attributes.get("body-omitted") == "true":
        marker = {
            "body_omitted": True,
            "gitlab_event": message.attributes.get("X-Gitlab-Event", ""),
            "publish_time": message.publish_time,
        }
        return IngestionRecord(
            source=IncomingEvent.Source.GITLAB, idempotency_key=key, payload_json=marker, settled=True
        )
    try:
        payload = json.loads(message.data)
    except ValueError:
        return _dead_letter(message, "message data is not JSON")
    if not isinstance(payload, dict):
        return _dead_letter(message, "message data is not a JSON object")
    return IngestionRecord(
        source=IncomingEvent.Source.GITLAB,
        idempotency_key=key,
        actor=_text(payload, "user", "username"),
        channel_ref=_text(payload, "project", "path_with_namespace"),
        thread_ref=_text(payload, "object_attributes", "iid"),
        body=_text(payload, "object_kind"),
        payload_json=payload,
        settled=True,
    )


def _dead_letter(message: PubsubMessage, reason: str) -> IngestionRecord:
    logger.warning("gitlab events: message %s stored as dead letter (%s)", message.message_id, reason)
    return IngestionRecord(
        source=IncomingEvent.Source.GITLAB,
        idempotency_key=f"gitlab:pubsub:{message.message_id}",
        payload_json={
            "attributes": message.attributes,
            "data": message.data[:DEAD_LETTER_DATA_BYTES].decode(errors="replace"),
            "publish_time": message.publish_time,
        },
        dead_letter_reason=reason,
    )


@dataclass(slots=True)
class _Stored:
    ack_ids: list[str]
    new: int = 0
    dead_lettered: int = 0
    first_failure: str = ""


@dataclass(slots=True)
class GitLabEventsScanner:
    subscription: str
    name: str = "gitlab_events"

    def scan(self) -> list[ScanSignal]:
        pubsub = PubsubSubscription(self.subscription, scanner=self.name)
        pulled = new = 0
        for _ in range(MAX_PULLS_PER_TICK):
            messages = pubsub.pull(PULL_BATCH)
            stored = self._store(messages)
            pubsub.acknowledge(stored.ack_ids)
            pulled += len(messages)
            new += stored.new
            if stored.first_failure:
                raise ScannerError(
                    scanner=self.name,
                    error_class=ScannerErrorClass.UNKNOWN,
                    detail=f"store failed for {stored.first_failure}, not acknowledged",
                )
            if messages and stored.dead_lettered == len(messages):
                raise ScannerError(
                    scanner=self.name,
                    error_class=ScannerErrorClass.UNKNOWN,
                    detail=f"all {len(messages)} messages of a pull were dead-lettered (relay contract changed?)",
                )
            if not messages:
                break
        if pulled:
            logger.info("gitlab events: pulled %d, stored %d, duplicates %d", pulled, new, pulled - new)
        return []

    @staticmethod
    def _store(messages: list[PubsubMessage]) -> _Stored:
        stored = _Stored(ack_ids=[])
        for message in messages:
            record = _record_for(message)
            try:
                is_new = persist_incoming_event(record)
            except DatabaseError as exc:
                logger.warning(
                    "gitlab events: store failed for %s, not acknowledged", record.idempotency_key, exc_info=True
                )
                stored.first_failure = stored.first_failure or f"{record.idempotency_key} ({type(exc).__name__})"
                continue
            stored.ack_ids.append(message.ack_id)
            stored.new += is_new
            stored.dead_lettered += bool(record.dead_letter_reason)
        return stored
