"""Behaviour tests for ``teatree.core.models.IncomingEvent``.

The model is the canonical ingestion record for external webhook traffic
(Slack, GitLab, GitHub, Notion, CI). Phase 1 of issue #654 — the model
exists ahead of the webhook views so downstream phases (classifier,
dispatcher branch) have a stable persistence layer to build on.
"""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import IncomingEvent
from teatree.core.models.incoming_event import MAX_INGEST_ATTEMPTS


class TestIncomingEvent(TestCase):
    def _payload(self, **overrides: object) -> dict:
        base = {
            "source": IncomingEvent.Source.SLACK,
            "actor": "U0123",
            "channel_ref": "C-eng",
            "thread_ref": "1234567890.0001",
            "body": "hey, can you ship MR !42 when CI goes green?",
            "payload_json": {"event_id": "Ev0000ABCD"},
            "idempotency_key": "slack:Ev0000ABCD",
        }
        base.update(overrides)
        return base

    def test_persists_required_fields_and_sets_received_at(self) -> None:
        event = IncomingEvent.objects.create(**self._payload())

        event.refresh_from_db()
        assert event.source == IncomingEvent.Source.SLACK
        assert event.actor == "U0123"
        assert event.channel_ref == "C-eng"
        assert event.thread_ref == "1234567890.0001"
        assert "MR !42" in event.body
        assert event.payload_json == {"event_id": "Ev0000ABCD"}
        assert event.idempotency_key == "slack:Ev0000ABCD"
        assert event.received_at is not None
        assert event.processed_at is None

    def test_idempotency_key_is_unique(self) -> None:
        IncomingEvent.objects.create(**self._payload())
        with pytest.raises(IntegrityError):
            IncomingEvent.objects.create(**self._payload())

    def test_source_rejects_unknown_value(self) -> None:
        event = IncomingEvent(**self._payload(source="email"))
        with pytest.raises(ValidationError):
            event.full_clean()

    def test_default_str_reads_source_and_idempotency_key(self) -> None:
        event = IncomingEvent.objects.create(**self._payload())
        assert "slack" in str(event)
        assert "Ev0000ABCD" in str(event)

    def test_mark_processed_sets_processed_at(self) -> None:
        event = IncomingEvent.objects.create(**self._payload())
        assert event.processed_at is None

        event.mark_processed()

        event.refresh_from_db()
        assert event.processed_at is not None

    def test_unprocessed_manager_excludes_completed_events(self) -> None:
        unprocessed = IncomingEvent.objects.create(**self._payload())
        processed = IncomingEvent.objects.create(**self._payload(idempotency_key="slack:Ev9999XYZ"))
        processed.mark_processed()

        unprocessed_pks = list(IncomingEvent.objects.unprocessed().values_list("pk", flat=True))

        assert unprocessed.pk in unprocessed_pks
        assert processed.pk not in unprocessed_pks

    def test_parent_fields_default_blank(self) -> None:
        event = IncomingEvent.objects.create(**self._payload())

        event.refresh_from_db()
        assert event.parent_ts == ""
        assert event.parent_text == ""
        assert event.is_thread_reply is False

    def test_reply_persists_parent_ts_and_text(self) -> None:
        event = IncomingEvent.objects.create(
            **self._payload(
                parent_ts="1234567890.0001",
                parent_text="approve posting the evidence?",
            )
        )

        event.refresh_from_db()
        assert event.parent_ts == "1234567890.0001"
        assert event.parent_text == "approve posting the evidence?"
        assert event.is_thread_reply is True


class TestIncomingEventReliability(TestCase):
    """The #673 retry / dead-letter reliability layer on failed drains."""

    def _event(self, key: str = "slack:Ev-fail") -> IncomingEvent:
        return IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            channel_ref="C-eng",
            idempotency_key=key,
        )

    def test_record_failure_increments_attempts_and_schedules_backoff_retry(self) -> None:
        event = self._event()

        dead = event.record_failure("boom")

        assert dead is False
        event.refresh_from_db()
        assert event.attempts == 1
        assert event.last_error == "boom"
        assert event.next_retry_at is not None
        assert event.dead_lettered_at is None
        assert event.processed_at is None  # a retryable failure is NOT silently dropped

    def test_backoff_grows_across_attempts(self) -> None:
        event = self._event()
        now = timezone.now()

        event.record_failure("e1", now=now)
        first_gap = event.next_retry_at - now
        event.record_failure("e2", now=now)
        second_gap = event.next_retry_at - now

        assert second_gap > first_gap

    def test_dead_letters_after_max_attempts(self) -> None:
        event = self._event()

        for _ in range(MAX_INGEST_ATTEMPTS - 1):
            assert event.record_failure("still failing") is False
        dead = event.record_failure("final")

        assert dead is True
        event.refresh_from_db()
        assert event.is_dead_lettered is True
        assert event.dead_lettered_at is not None
        assert event.next_retry_at is None

    def test_unprocessed_excludes_dead_lettered_events(self) -> None:
        alive = self._event("slack:alive")
        poison = self._event("slack:poison")
        for _ in range(MAX_INGEST_ATTEMPTS):
            poison.record_failure("boom")

        pks = list(IncomingEvent.objects.unprocessed().values_list("pk", flat=True))

        assert alive.pk in pks
        assert poison.pk not in pks

    def test_unprocessed_excludes_events_not_yet_due_for_retry(self) -> None:
        event = self._event()
        event.record_failure("boom")  # schedules next_retry_at 30s out

        now = timezone.now()
        not_yet = list(IncomingEvent.objects.unprocessed(now=now).values_list("pk", flat=True))
        later = list(IncomingEvent.objects.unprocessed(now=now + timedelta(hours=2)).values_list("pk", flat=True))

        assert event.pk not in not_yet
        assert event.pk in later

    def test_dead_lettered_query_lists_only_dead_letters(self) -> None:
        alive = self._event("slack:alive")
        poison = self._event("slack:poison")
        for _ in range(MAX_INGEST_ATTEMPTS):
            poison.record_failure("boom")

        dead_pks = list(IncomingEvent.objects.dead_lettered().values_list("pk", flat=True))

        assert dead_pks == [poison.pk]
        assert alive.pk not in dead_pks
