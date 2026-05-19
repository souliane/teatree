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
        with patch("teatree.core.notify.messaging_from_overlay", return_value=None):
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

    def test_empty_channel_from_open_dm_is_hard_failure(self) -> None:
        """``open_dm`` returns ``""`` on Slack ``ok:false`` (e.g. missing_scope).

        The bug this guards: the empty channel was treated as benign, the
        DM never landed, yet ``BotPing`` was marked ``SENT`` and the
        function returned ``True`` — a silent success. The contract is a
        HARD FAILURE: ``FAILED`` row, ``False`` return, no claim recorded.
        """
        backend = _backend()
        backend.open_dm.return_value = ""

        sent = notify_user(
            "this never lands",
            kind=NotifyKind.INFO,
            idempotency_key="empty-channel",
            backend=backend,
            user_id="U_ME",
        )

        assert sent is False
        backend.post_message.assert_not_called()
        row = BotPing.objects.get(idempotency_key="empty-channel")
        assert row.status == BotPing.Status.FAILED
        assert "open_dm" in row.error_message or "channel" in row.error_message
        from teatree.core.models import OutboundClaim  # noqa: PLC0415

        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:empty-channel").exists()

    def test_post_message_ok_false_is_hard_failure(self) -> None:
        """``post_message`` returning ``{"ok": false}`` must be a HARD FAILURE.

        Slack returns ``ok:false`` (missing_scope, channel_not_found, …)
        with no ``ts``. The pre-fix code recorded ``SENT`` + returned
        ``True`` because it only looked at ``response.get("ts")``.
        """
        backend = _backend()
        backend.post_message.return_value = {"ok": False, "error": "missing_scope"}

        sent = notify_user(
            "not actually posted",
            kind=NotifyKind.INFO,
            idempotency_key="ok-false",
            backend=backend,
            user_id="U_ME",
        )

        assert sent is False
        row = BotPing.objects.get(idempotency_key="ok-false")
        assert row.status == BotPing.Status.FAILED
        assert "missing_scope" in row.error_message
        from teatree.core.models import OutboundClaim  # noqa: PLC0415

        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:ok-false").exists()

    def test_post_message_empty_ts_is_hard_failure(self) -> None:
        """An ``ok:true`` response with no ``ts`` still means nothing landed."""
        backend = _backend()
        backend.post_message.return_value = {"ok": True}

        sent = notify_user(
            "phantom success",
            kind=NotifyKind.INFO,
            idempotency_key="empty-ts",
            backend=backend,
            user_id="U_ME",
        )

        assert sent is False
        row = BotPing.objects.get(idempotency_key="empty-ts")
        assert row.status == BotPing.Status.FAILED
        from teatree.core.models import OutboundClaim  # noqa: PLC0415

        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:empty-ts").exists()

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

        with patch("teatree.core.notify.get_effective_settings", return_value=fake_settings):
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


class TestNotifyUserLinkify(TestCase):
    """Slack mrkdwn rewrite is applied to the payload, not the audit row."""

    def test_dashboard_md_links_become_mrkdwn(self) -> None:
        backend = _backend()
        text = "see [the PR](https://example.com/pr/1) for details"
        sent = notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key="linkify-md",
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        sent_text = backend.post_message.call_args.kwargs["text"]
        assert "<https://example.com/pr/1|the PR>" in sent_text
        # The original markdown form is GONE from the payload
        assert "[the PR](https://example.com/pr/1)" not in sent_text
        # The audit row keeps the original
        row = BotPing.objects.get(idempotency_key="linkify-md")
        assert row.text == text

    def test_bare_mr_token_uses_overlay_resolver(self) -> None:
        backend = _backend()
        fake_overlay = MagicMock()
        fake_overlay.resolve_mr_token.side_effect = lambda n: (
            f"https://gitlab.example.com/group/repo-a/-/merge_requests/{n}" if n == 281 else None
        )
        fake_overlay.resolve_issue_token.return_value = None

        with patch("teatree.core.overlay_loader.get_overlay", return_value=fake_overlay):
            notify_user(
                "approve !281 then !999",
                kind=NotifyKind.INFO,
                idempotency_key="linkify-mr",
                backend=backend,
                user_id="U_ME",
            )

        sent_text = backend.post_message.call_args.kwargs["text"]
        assert "<https://gitlab.example.com/group/repo-a/-/merge_requests/281|!281>" in sent_text
        # Unresolved token stays bare — better inert than wrong
        assert "!999" in sent_text
        assert "<https://gitlab.example.com/group/repo-a/-/merge_requests/999" not in sent_text

    def test_linkify_false_opts_out(self) -> None:
        backend = _backend()
        text = "raw [link](https://example.com) stays raw"
        notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key="linkify-off",
            backend=backend,
            user_id="U_ME",
            linkify=False,
        )

        sent_text = backend.post_message.call_args.kwargs["text"]
        assert "[link](https://example.com)" in sent_text
        assert "<https://example.com|link>" not in sent_text

    def test_overlay_resolution_failure_does_not_break_send(self) -> None:
        backend = _backend()
        with patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("no overlay")):
            sent = notify_user(
                "ship [the PR](https://example.com/pr/1) and !281",
                kind=NotifyKind.INFO,
                idempotency_key="linkify-overlay-fail",
                backend=backend,
                user_id="U_ME",
            )

        assert sent is True
        sent_text = backend.post_message.call_args.kwargs["text"]
        # Markdown link rewrite still works (no overlay needed)
        assert "<https://example.com/pr/1|the PR>" in sent_text
        # Bare MR token stayed bare because no resolver
        assert "!281" in sent_text

    def test_workaround_user_id_kwarg_still_supported(self) -> None:
        """``notify_user(user_id="U0DEMOUSER1")`` workaround must still work."""
        backend = _backend()
        sent = notify_user(
            "ping",
            kind=NotifyKind.INFO,
            idempotency_key="user-id-workaround",
            backend=backend,
            user_id="U0DEMOUSER1",
        )

        assert sent is True
        backend.open_dm.assert_called_once_with("U0DEMOUSER1")
