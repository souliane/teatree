"""Behaviour tests for ReplyDispatch + Replier protocol (#654 phase 4)."""

from django.test import TestCase

from teatree.core.models import IncomingEvent, ReplyDispatch
from teatree.core.reply_transport import NoopReplier, Replier


class TestReplyDispatch(TestCase):
    def test_persists_with_canonical_status_choices(self) -> None:
        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            body="hi",
            idempotency_key="slack:rd-1",
        )

        dispatch = ReplyDispatch.objects.create(
            event=event,
            target_ref="C-eng",
            action_name="post_in_thread",
            idempotency_key="slack:rd-1:C-eng:post_in_thread",
            status=ReplyDispatch.Status.PENDING,
        )

        assert dispatch.pk is not None
        choices = {value for value, _label in ReplyDispatch.Status.choices}
        assert choices == {"pending", "sent", "failed"}

    def test_idempotency_key_is_unique(self) -> None:
        from django.db import IntegrityError  # noqa: PLC0415

        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            body="x",
            idempotency_key="slack:rd-2",
        )
        ReplyDispatch.objects.create(
            event=event,
            target_ref="C-eng",
            action_name="post_in_thread",
            idempotency_key="dup-key",
            status=ReplyDispatch.Status.PENDING,
        )

        try:
            ReplyDispatch.objects.create(
                event=event,
                target_ref="C-eng",
                action_name="post_in_thread",
                idempotency_key="dup-key",
                status=ReplyDispatch.Status.PENDING,
            )
        except IntegrityError:
            return
        msg = "Expected IntegrityError on duplicate idempotency_key"
        raise AssertionError(msg)


class TestNoopReplier(TestCase):
    def test_post_in_thread_records_dispatch_as_sent(self) -> None:
        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            body="x",
            idempotency_key="slack:noop-1",
        )
        replier: Replier = NoopReplier()

        dispatch = replier.post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello",
            idempotency_key="slack:noop-1:C-eng:thread",
        )

        assert dispatch.status == ReplyDispatch.Status.SENT
        assert dispatch.target_ref == "C-eng/t1"

    def test_post_in_thread_is_idempotent_on_duplicate_key(self) -> None:
        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            body="x",
            idempotency_key="slack:noop-2",
        )
        replier: Replier = NoopReplier()

        first = replier.post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello",
            idempotency_key="slack:noop-2:reply",
        )
        second = replier.post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello again",
            idempotency_key="slack:noop-2:reply",
        )

        assert first.pk == second.pk
        assert ReplyDispatch.objects.filter(idempotency_key="slack:noop-2:reply").count() == 1
