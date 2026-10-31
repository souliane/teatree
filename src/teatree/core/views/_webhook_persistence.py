"""Atomic ingest of normalized inbound records with duplicate suppression."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Protocol

from django.db import IntegrityError, OperationalError, transaction
from django.http import HttpResponse
from django.utils import timezone

from teatree.core.modelkit.db_retry import is_locked_error
from teatree.core.models import IncomingEvent
from teatree.core.models.provenance import classify_provenance

logger = logging.getLogger(__name__)

REPLAY_WINDOW = timedelta(days=7)


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


class IngestDispatch(Protocol):
    def handles(self, record: "IngestionRecord") -> bool: ...

    def wake(self, event_pk: int) -> None: ...


@dataclass
class _IngestSeam:
    dispatch: IngestDispatch | None = None


_INGEST = _IngestSeam()


def set_ingest_dispatch(dispatch: IngestDispatch | None) -> None:
    _INGEST.dispatch = dispatch


def _handling_dispatch(record: IngestionRecord) -> IngestDispatch | None:
    dispatch = _INGEST.dispatch
    if dispatch is None or record.dead_letter_reason or not dispatch.handles(record):
        return None
    return dispatch


def _is_replay(record: IngestionRecord, moment: datetime) -> bool:
    return (
        record.origin == IncomingEvent.Origin.WEBHOOK
        and record.occurred_at is not None
        and record.occurred_at < moment - REPLAY_WINDOW
    )


def _wake_after_commit(dispatch: IngestDispatch, event_pk: int) -> None:
    try:
        dispatch.wake(event_pk)
    except Exception:
        logger.warning(
            "wake for incoming event %s not enqueued; the scanner drain will handle it", event_pk, exc_info=True
        )


def ingest(record: IngestionRecord) -> IngestOutcome:
    # The single ingestion chokepoint every inbound flow passes through (both webhook
    # views + the GitLab Pub/Sub scanner), so the #116 provenance is stamped ONCE
    # here from (source, actor) — the views need no change.
    provenance = classify_provenance(record.source, record.actor)
    moment = timezone.now()
    handling = _handling_dispatch(record)
    replayed = handling is not None and _is_replay(record, moment)
    if replayed:
        handling = None
    settled = replayed or (record.settled and handling is None)
    trim = IncomingEvent.objects.is_settled_forge(
        record.source, settled=settled, dead_lettered=bool(record.dead_letter_reason)
    )
    try:
        with transaction.atomic():
            event = IncomingEvent.objects.create(
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
                processed_at=moment if settled else None,
            )
            if handling is not None:
                transaction.on_commit(partial(_wake_after_commit, handling, event.pk))
    except IntegrityError:
        logger.debug("%s already ingested — replay suppressed", record.idempotency_key)
        return IngestOutcome.DUPLICATE
    return IngestOutcome.NEW


def persist_incoming_event(record: IngestionRecord) -> bool:
    return ingest(record) is IngestOutcome.NEW


def ingest_response(record: IngestionRecord) -> HttpResponse:
    try:
        persist_incoming_event(record)
    except OperationalError as exc:
        if not is_locked_error(exc):
            raise
        logger.warning("%s not stored: the database is locked, answered 503", record.idempotency_key)
        return HttpResponse(status=503)
    return HttpResponse(status=200)
