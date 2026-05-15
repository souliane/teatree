"""Behaviour tests for IncomingEventsScanner (#669)."""

from django.test import TestCase

from teatree.core.models import IncomingEvent, ReplyDispatch
from teatree.loop.scanners.incoming_events import IncomingEventsScanner


def _event(*, source: str, body: str, key: str, **payload_extras: object) -> IncomingEvent:
    return IncomingEvent.objects.create(
        source=source,
        actor="alice",
        channel_ref="C-eng",
        thread_ref="thread-1",
        body=body,
        payload_json=payload_extras,
        idempotency_key=key,
    )


class TestIncomingEventsScanner(TestCase):
    def test_marks_record_only_events_as_processed(self) -> None:
        event = _event(
            source=IncomingEvent.Source.CI,
            body="pipeline succeeded",
            key="ci:1",
            status="success",
        )

        signals = IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.processed_at is not None
        assert any(s.kind == "incoming_event.recorded" for s in signals)

    def test_drops_noise_and_marks_processed(self) -> None:
        event = _event(
            source=IncomingEvent.Source.SLACK,
            body="",
            key="slack:noise",
            event={"type": "team_join"},
        )

        IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.processed_at is not None

    def test_alert_user_dispatches_via_replier(self) -> None:
        event = _event(
            source=IncomingEvent.Source.SLACK,
            body="<@bot> urgent: prod is down",
            key="slack:urgent",
            event={"type": "app_mention"},
        )

        IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.processed_at is not None
        assert ReplyDispatch.objects.filter(event=event, action_name="post_dm").exists()

    def test_schedule_task_emits_action_signal(self) -> None:
        event = _event(
            source=IncomingEvent.Source.SLACK,
            body="<@bot> please implement the dashboard",
            key="slack:task1",
            event={"type": "app_mention"},
        )

        signals = IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.processed_at is not None
        kinds = {s.kind for s in signals}
        assert "incoming_event.task_needed" in kinds

    def test_schedule_merge_emits_action_signal(self) -> None:
        event = _event(
            source=IncomingEvent.Source.GITLAB,
            body="approved",
            key="gitlab:approval",
            object_kind="merge_request",
            object_attributes={"action": "approved", "iid": 42},
        )

        signals = IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.processed_at is not None
        kinds = {s.kind for s in signals}
        assert "incoming_event.merge_needed" in kinds

    def test_skip_already_processed_events(self) -> None:
        event = _event(
            source=IncomingEvent.Source.CI,
            body="pipeline succeeded",
            key="ci:already",
        )
        event.mark_processed()

        signals = IncomingEventsScanner().scan()

        assert signals == []

    def test_one_corrupt_event_does_not_block_the_queue(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        corrupt = _event(source=IncomingEvent.Source.CI, body="boom", key="ci:corrupt")
        healthy = _event(source=IncomingEvent.Source.CI, body="ok", key="ci:healthy")

        original = IncomingEventsScanner._handle

        def _maybe_explode(self: IncomingEventsScanner, event: IncomingEvent):
            if event.pk == corrupt.pk:
                msg = "synthetic failure"
                raise RuntimeError(msg)
            return original(self, event)

        with patch.object(IncomingEventsScanner, "_handle", _maybe_explode):
            IncomingEventsScanner().scan()

        corrupt.refresh_from_db()
        healthy.refresh_from_db()
        assert corrupt.processed_at is not None
        assert healthy.processed_at is not None

    def test_respects_limit(self) -> None:
        for i in range(5):
            _event(
                source=IncomingEvent.Source.CI,
                body=f"pipeline {i}",
                key=f"ci:limit-{i}",
            )

        signals = IncomingEventsScanner(limit=2).scan()

        processed_count = IncomingEvent.objects.filter(processed_at__isnull=False).count()
        assert processed_count == 2
        assert len(signals) <= 2

    def test_unmigrated_db_is_a_silent_noop(self) -> None:
        """A present-but-un-migrated DB is a silent no-op.

        With the `teatree_incoming_event` table genuinely absent (the
        pre-migration install state), `scan()` returns `[]` instead of
        raising the real engine error that `tick._run_job` would surface
        as a per-tick WARN. Drops the real table with raw DDL so the
        production query hits the real missing-relation exception (per
        AGENTS.md Test-Writing Doctrine — real DB, not a mocked
        manager); the TestCase transaction rolls the DROP back.
        """
        from django.db import connection  # noqa: PLC0415

        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE teatree_incoming_event")

        signals = IncomingEventsScanner().scan()

        assert signals == []
