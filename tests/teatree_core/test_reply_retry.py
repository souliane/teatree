"""Behaviour tests for the failed-ReplyDispatch retry sweep (#673 items 1+2)."""

import datetime as dt
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import IncomingEvent, ReplyDispatch
from teatree.core.reply_retry import sweep_failed_dispatches
from teatree.core.reply_transport import NoopReplier


def _event(key: str = "slack:e1") -> IncomingEvent:
    return IncomingEvent.objects.create(
        source=IncomingEvent.Source.SLACK,
        actor="U_ALICE",
        channel_ref="C-eng",
        thread_ref="t1",
        body="orig",
        idempotency_key=key,
    )


def _failed(event: IncomingEvent, *, key: str, retry_count: int = 0, **extra: object) -> ReplyDispatch:
    return ReplyDispatch.objects.create(
        event=event,
        target_ref="C-eng",
        action_name="post_in_thread",
        idempotency_key=key,
        status=ReplyDispatch.Status.FAILED,
        error_message="boom",
        body="hello",
        retry_count=retry_count,
        **extra,
    )


class TestReplyDispatchRetryFields(TestCase):
    def test_dead_letter_status_exists(self) -> None:
        names = {v for v, _ in ReplyDispatch.Status.choices}
        assert names == {"pending", "sent", "failed", "dead_letter"}

    def test_due_for_retry_includes_failed_with_no_next_retry(self) -> None:
        event = _event()
        d = _failed(event, key="k1")
        assert d in list(ReplyDispatch.objects.due_for_retry())

    def test_due_for_retry_excludes_future_next_retry(self) -> None:
        event = _event()
        future = timezone.now() + dt.timedelta(hours=1)
        d = _failed(event, key="k2", next_retry_at=future)
        assert d not in list(ReplyDispatch.objects.due_for_retry())

    def test_due_for_retry_excludes_sent_and_dead_letter(self) -> None:
        event = _event()
        sent = _failed(event, key="k3")
        sent.status = ReplyDispatch.Status.SENT
        sent.save()
        dead = _failed(event, key="k4")
        dead.status = ReplyDispatch.Status.DEAD_LETTER
        dead.save()
        due = list(ReplyDispatch.objects.due_for_retry())
        assert sent not in due
        assert dead not in due


class TestSweepFailedDispatches(TestCase):
    def test_successful_retry_flips_to_sent(self) -> None:
        event = _event()
        d = _failed(event, key="k1")
        replier = MagicMock()

        swept = sweep_failed_dispatches(resolver=lambda _src: replier)

        d.refresh_from_db()
        assert d.status == ReplyDispatch.Status.SENT
        assert d.error_message == ""
        assert swept == 1
        replier.redeliver.assert_called_once()

    def test_failed_retry_increments_count_and_backs_off(self) -> None:
        event = _event()
        d = _failed(event, key="k1", retry_count=1)
        replier = MagicMock()
        replier.redeliver.side_effect = RuntimeError("still down")
        before = timezone.now()

        sweep_failed_dispatches(resolver=lambda _src: replier, max_retries=5)

        d.refresh_from_db()
        assert d.status == ReplyDispatch.Status.FAILED
        assert d.retry_count == 2
        assert "still down" in d.error_message
        assert d.next_retry_at is not None
        assert d.next_retry_at > before

    def test_exhausted_retries_dead_letters_and_alerts(self) -> None:
        event = _event()
        d = _failed(event, key="k1", retry_count=4)
        replier = MagicMock()
        replier.redeliver.side_effect = RuntimeError("permanently down")

        sweep_failed_dispatches(resolver=lambda _src: replier, max_retries=5)

        d.refresh_from_db()
        assert d.status == ReplyDispatch.Status.DEAD_LETTER
        replier.post_dm.assert_called_once()
        _, kwargs = replier.post_dm.call_args
        assert kwargs["actor"] == "U_ALICE"
        assert "permanently down" in kwargs["body"]
        assert str(event.pk) in kwargs["body"]

    def test_dead_letter_alert_failure_does_not_crash_sweep(self) -> None:
        event = _event()
        d = _failed(event, key="k1", retry_count=4)
        replier = MagicMock()
        replier.redeliver.side_effect = RuntimeError("permanently down")
        replier.post_dm.side_effect = RuntimeError("DM channel also broken")

        swept = sweep_failed_dispatches(resolver=lambda _src: replier, max_retries=5)

        d.refresh_from_db()
        assert d.status == ReplyDispatch.Status.DEAD_LETTER
        assert swept == 1

    def test_dead_letter_alert_failure_persists_auditable_row(self) -> None:
        """A permanently-broken DM channel leaves an auditable record.

        Rather than silently dropping the dead letter.
        """
        event = _event()
        d = _failed(event, key="k1", retry_count=4)
        replier = MagicMock()
        replier.redeliver.side_effect = RuntimeError("permanently down")
        replier.post_dm.side_effect = RuntimeError("DM channel also broken")

        sweep_failed_dispatches(resolver=lambda _src: replier, max_retries=5)

        audit = ReplyDispatch.objects.get(idempotency_key=f"{d.idempotency_key}:deadletter-alert")
        assert audit.status == ReplyDispatch.Status.FAILED
        assert "DM channel also broken" in audit.error_message
        # Excluded from the retry sweep — a broken DM channel must not storm.
        assert audit not in list(ReplyDispatch.objects.due_for_retry())

    def test_dead_letter_alert_audit_row_is_idempotent(self) -> None:
        """Re-running the sweep must not crash on the unique idempotency key.

        Holds when a dead-letter-alert audit row already exists.
        """
        event = _event()
        d = _failed(event, key="k1", retry_count=4)
        ReplyDispatch.objects.create(
            event=event,
            target_ref="U_ALICE",
            action_name="dead_letter_alert",
            idempotency_key=f"{d.idempotency_key}:deadletter-alert",
            status=ReplyDispatch.Status.FAILED,
            body="prior audit row",
        )
        replier = MagicMock()
        replier.redeliver.side_effect = RuntimeError("permanently down")
        replier.post_dm.side_effect = RuntimeError("DM channel also broken")

        swept = sweep_failed_dispatches(resolver=lambda _src: replier, max_retries=5)

        d.refresh_from_db()
        assert d.status == ReplyDispatch.Status.DEAD_LETTER
        assert swept == 1
        assert ReplyDispatch.objects.filter(idempotency_key=f"{d.idempotency_key}:deadletter-alert").count() == 1

    def test_post_success_save_failure_is_logged(self) -> None:
        """A post-success / save-failure is logged explicitly.

        Elevated from a docstring caveat to a logged WARNING.
        """
        event = _event()
        _failed(event, key="k1")
        replier = MagicMock()

        with (
            patch.object(ReplyDispatch, "save", side_effect=RuntimeError("db gone")),
            self.assertLogs("teatree.core.reply_retry", level="WARNING") as logs,
        ):
            sweep_failed_dispatches(resolver=lambda _src: replier)

        assert any("db gone" in m or "could not be saved" in m for m in logs.output)

    def test_no_replier_leaves_dispatch_untouched(self) -> None:
        event = _event()
        d = _failed(event, key="k1")

        swept = sweep_failed_dispatches(resolver=lambda _src: None)

        d.refresh_from_db()
        assert d.status == ReplyDispatch.Status.FAILED
        assert d.retry_count == 0
        assert swept == 0

    def test_dead_letter_alert_rows_are_not_swept(self) -> None:
        event = _event()
        alert = ReplyDispatch.objects.create(
            event=event,
            target_ref="U_ALICE",
            action_name="dead_letter_alert",
            idempotency_key="k1:deadletter",
            status=ReplyDispatch.Status.FAILED,
            body="x",
        )
        assert alert not in list(ReplyDispatch.objects.due_for_retry())

    def test_respects_limit(self) -> None:
        event = _event()
        for i in range(4):
            _failed(event, key=f"k{i}")
        replier = MagicMock()

        swept = sweep_failed_dispatches(resolver=lambda _src: replier, limit=2)

        assert swept == 2


class TestRecordPersistsBody(TestCase):
    def test_noop_replier_persists_body_for_retry(self) -> None:
        event = _event()
        dispatch = NoopReplier().post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="the original message",
            idempotency_key="slack:body:1",
        )
        dispatch.refresh_from_db()
        assert dispatch.body == "the original message"
