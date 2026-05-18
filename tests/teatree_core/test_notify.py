"""Behaviour tests for the bot→user notification helper (#963)."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.models import BotPing
from teatree.notify import NotifyKind, notify_user


def _backend(*, permalink: str = "https://acme.slack.com/archives/D-USER/p1700000000000000") -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = permalink
    return b


class TestNotifyUser(TestCase):
    def test_happy_path_posts_dm_and_records_audit_with_permalink(self) -> None:
        backend = _backend()
        sent = notify_user(
            "tests are green",
            kind=NotifyKind.INFO,
            idempotency_key="sess=a;turn=1",
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        backend.open_dm.assert_called_once_with("U_ME")
        backend.post_message.assert_called_once()
        call_kwargs = backend.post_message.call_args.kwargs
        assert call_kwargs["channel"] == "D-USER"
        assert call_kwargs["thread_ts"] == ""
        assert "info" in call_kwargs["text"].lower()
        assert "tests are green" in call_kwargs["text"]

        backend.get_permalink.assert_called_once_with(channel="D-USER", ts="1700000000.000000")

        row = BotPing.objects.get(idempotency_key="sess=a;turn=1")
        assert row.status == BotPing.Status.SENT
        assert row.kind == BotPing.Kind.INFO
        assert row.channel_ref == "D-USER"
        assert row.posted_ts == "1700000000.000000"
        assert row.permalink.startswith("https://")
        assert row.text == "tests are green"

    def test_kind_accepts_string_alias(self) -> None:
        backend = _backend()
        sent = notify_user(
            "draft reply ready",
            kind="answer",
            idempotency_key="alias-str",
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        assert BotPing.objects.get(idempotency_key="alias-str").kind == BotPing.Kind.ANSWER

    def test_duplicate_idempotency_key_is_noop(self) -> None:
        backend = _backend()
        notify_user(
            "first",
            kind=NotifyKind.INFO,
            idempotency_key="dup",
            backend=backend,
            user_id="U_ME",
        )
        sent2 = notify_user(
            "second",
            kind=NotifyKind.INFO,
            idempotency_key="dup",
            backend=backend,
            user_id="U_ME",
        )

        assert sent2 is True  # the prior send was SENT
        assert backend.post_message.call_count == 1
        assert BotPing.objects.filter(idempotency_key="dup").count() == 1

    def test_missing_backend_records_noop_and_returns_false(self) -> None:
        with patch("teatree.notify.messaging_from_overlay", return_value=None):
            sent = notify_user(
                "no backend",
                kind=NotifyKind.QUESTION,
                idempotency_key="noop-backend",
                backend=None,
                user_id="U_ME",
            )

        assert sent is False
        row = BotPing.objects.get(idempotency_key="noop-backend")
        assert row.status == BotPing.Status.NOOP
        assert row.permalink == ""

    def test_missing_user_id_records_noop_and_returns_false(self) -> None:
        backend = _backend()
        sent = notify_user(
            "no user id",
            kind=NotifyKind.QUESTION,
            idempotency_key="noop-uid",
            backend=backend,
            user_id="",
        )

        assert sent is False
        backend.open_dm.assert_not_called()
        row = BotPing.objects.get(idempotency_key="noop-uid")
        assert row.status == BotPing.Status.NOOP

    def test_transport_exception_is_swallowed_and_audited(self) -> None:
        backend = _backend()
        backend.post_message.side_effect = RuntimeError("slack timeout")
        sent = notify_user(
            "boom",
            kind=NotifyKind.INFO,
            idempotency_key="failed",
            backend=backend,
            user_id="U_ME",
        )

        assert sent is False
        row = BotPing.objects.get(idempotency_key="failed")
        assert row.status == BotPing.Status.FAILED
        assert "slack timeout" in row.error_message

    def test_permalink_lookup_failure_does_not_break_send(self) -> None:
        backend = _backend()
        backend.get_permalink.side_effect = RuntimeError("permalink api down")
        sent = notify_user(
            "still sent",
            kind=NotifyKind.INFO,
            idempotency_key="permalink-fail",
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        row = BotPing.objects.get(idempotency_key="permalink-fail")
        assert row.status == BotPing.Status.SENT
        assert row.permalink == ""

    def test_feature_disabled_returns_false_without_calling_backend(self) -> None:
        backend = _backend()
        fake_settings = MagicMock()
        fake_settings.notify_user_via_bot = False

        with patch("teatree.notify.get_effective_settings", return_value=fake_settings):
            sent = notify_user(
                "shh",
                kind=NotifyKind.INFO,
                idempotency_key="disabled",
                backend=backend,
                user_id="U_ME",
            )

        assert sent is False
        backend.open_dm.assert_not_called()
        assert not BotPing.objects.filter(idempotency_key="disabled").exists()
