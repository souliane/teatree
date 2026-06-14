"""Behaviour tests for the bot→user notification helper (#963)."""

from unittest.mock import MagicMock, patch

from django.db import OperationalError
from django.test import TestCase

from teatree.core import notify as notify_module
from teatree.core.models import BotPing, IncomingEvent
from teatree.notify import NotifyKind, notify_user

_DB_LOCKED = OperationalError("database is locked")


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

    def test_threads_under_active_dm_thread_when_one_exists(self) -> None:
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            channel_ref="D-USER",
            thread_ref="1700000000.000111",
            idempotency_key="slack:Ev-active",
        )
        backend = _backend()

        notify_user(
            "answering your question",
            kind=NotifyKind.ANSWER,
            idempotency_key="threaded-answer",
            backend=backend,
            user_id="U_ME",
        )

        assert backend.post_message.call_args.kwargs["thread_ts"] == "1700000000.000111"

    def test_no_active_thread_posts_at_root(self) -> None:
        backend = _backend()

        notify_user(
            "first message in the conversation",
            kind=NotifyKind.INFO,
            idempotency_key="rootless",
            backend=backend,
            user_id="U_ME",
        )

        assert backend.post_message.call_args.kwargs["thread_ts"] == ""

    def test_active_thread_lookup_db_error_falls_back_to_root(self) -> None:
        backend = _backend()
        with patch(
            "teatree.core.models.IncomingEvent.objects.active_dm_thread",
            side_effect=OperationalError("database is locked"),
        ):
            sent = notify_user(
                "lookup blew up but the DM still lands",
                kind=NotifyKind.INFO,
                idempotency_key="thread-lookup-db-error",
                backend=backend,
                user_id="U_ME",
            )

        assert sent is True
        assert backend.post_message.call_args.kwargs["thread_ts"] == ""

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

    def test_reentrant_concurrent_tick_during_delivery_does_not_double_dm(self) -> None:
        """A second tick firing *during* the first tick's delivery must NOT also deliver.

        The pre-fix shape delivered BEFORE recording the SENT row, so a second
        tick re-reading in that window saw no SENT row, deleted any recoverable
        row, and delivered again — a double-DM. The fix claims a SENDING row
        atomically *before* delivering, so the re-entrant second tick's
        ``claim_delivery`` observes the in-flight claim and stands down.

        This drives the race deterministically: the first tick's ``open_dm``
        re-enters ``notify_user`` with the same key (the concurrent second
        tick) before the first tick has finalized. Exactly one
        ``post_message`` lands and exactly one SENT row survives.
        """
        backend = _backend()
        second_outcome: dict[str, bool] = {}

        def _reentrant_open_dm(user_id: str) -> str:
            if "second" not in second_outcome:
                second_outcome["second"] = notify_user(
                    "concurrent second tick",
                    kind=NotifyKind.INFO,
                    idempotency_key="toctou",
                    backend=backend,
                    user_id="U_ME",
                )
            return "D-USER"

        backend.open_dm.side_effect = _reentrant_open_dm

        first = notify_user(
            "first tick",
            kind=NotifyKind.INFO,
            idempotency_key="toctou",
            backend=backend,
            user_id="U_ME",
        )

        assert first is True
        assert second_outcome["second"] is False  # the re-entrant tick stood down
        assert backend.post_message.call_count == 1, "double-DM: delivery fired twice"
        assert BotPing.objects.filter(idempotency_key="toctou", status=BotPing.Status.SENT).count() == 1
        assert BotPing.objects.filter(idempotency_key="toctou").count() == 1

    def test_prior_failed_attempt_does_not_block_retry(self) -> None:
        """Regression for #1306: a FAILED BotPing must not block a sub-agent retry.

        Pre-fix the idempotency-key ledger was strict: ANY existing row
        (FAILED, NOOP, SENT) made the helper return the prior outcome
        without retrying. A sub-agent that hit a transient Slack failure
        was permanently locked out — the only escape was a fresh key.

        verify-by-re-read + idempotency means: SENT stays a no-op,
        FAILED/NOOP are recoverable. The prior failed row is replaced
        when delivery succeeds, so the audit trail still reflects the
        terminal outcome.
        """
        # First attempt — transport raises, ledger records FAILED.
        bad_backend = _backend()
        bad_backend.post_message.side_effect = RuntimeError("transient slack 500")
        assert (
            notify_user(
                "retry me",
                kind=NotifyKind.INFO,
                idempotency_key="retry-1306",
                backend=bad_backend,
                user_id="U_ME",
            )
            is False
        )
        assert BotPing.objects.get(idempotency_key="retry-1306").status == BotPing.Status.FAILED

        # Second attempt with a working backend — must actually deliver, not no-op.
        good_backend = _backend()
        assert (
            notify_user(
                "retry me",
                kind=NotifyKind.INFO,
                idempotency_key="retry-1306",
                backend=good_backend,
                user_id="U_ME",
            )
            is True
        )
        good_backend.open_dm.assert_called_once_with("U_ME")
        good_backend.post_message.assert_called_once()
        # The terminal ledger row reflects the eventual success.
        assert BotPing.objects.get(idempotency_key="retry-1306").status == BotPing.Status.SENT

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


class TestNotifyUserNeverRaises(TestCase):
    """The never-raise contract holds for any DatabaseError, not just IntegrityError.

    ``notify_user`` runs inside FSM transitions and the public docstring
    promises it never raises into the CLI turn. Pre-fix the SENT-audit
    write caught only ``IntegrityError``, so an ``OperationalError`` (e.g.
    SQLite "database is locked") raised by the ``BotPing`` write — after
    the DM had already landed — escaped and broke the caller's transition.
    The same too-narrow catch applied to the NOOP/FAILED audit writes and
    to the early delivery-claim. The most-defensive sibling
    (``_record_outbound_claim``) already swallows the full ``DatabaseError``
    breadth; every DB access in ``notify_user`` must match it so no
    ``DatabaseError`` reaches the caller.
    """

    def test_operational_error_on_delivery_claim_is_swallowed(self) -> None:
        backend = _backend()
        with patch.object(BotPing, "claim_delivery", side_effect=_DB_LOCKED):
            sent = notify_user(
                "delivery claim under lock contention",
                kind=NotifyKind.INFO,
                idempotency_key="db-locked-claim",
                backend=backend,
                user_id="U_ME",
            )

        # Fail closed: no delivery, no propagation, caller keeps moving.
        assert sent is False
        backend.post_message.assert_not_called()

    def test_operational_error_on_sent_finalize_is_swallowed(self) -> None:
        backend = _backend()
        with patch.object(BotPing, "finalize_sent", side_effect=_DB_LOCKED):
            sent = notify_user(
                "lock contention on the sent finalize",
                kind=NotifyKind.INFO,
                idempotency_key="db-locked-sent",
                backend=backend,
                user_id="U_ME",
            )

        # DM landed; the failed finalize write must not propagate or flip the result.
        assert sent is True
        backend.post_message.assert_called_once()

    def test_operational_error_on_noop_audit_is_swallowed(self) -> None:
        with (
            patch("teatree.core.notify.messaging_from_overlay", return_value=None),
            patch.object(BotPing.objects, "create", side_effect=_DB_LOCKED),
        ):
            sent = notify_user(
                "no backend, locked audit",
                kind=NotifyKind.QUESTION,
                idempotency_key="db-locked-noop",
                backend=None,
                user_id="U_ME",
            )

        assert sent is False

    def test_operational_error_on_failed_finalize_is_swallowed(self) -> None:
        backend = _backend()
        backend.post_message.side_effect = RuntimeError("slack timeout")
        with patch.object(BotPing, "finalize_failed", side_effect=_DB_LOCKED):
            sent = notify_user(
                "delivery failed, locked finalize",
                kind=NotifyKind.INFO,
                idempotency_key="db-locked-failed",
                backend=backend,
                user_id="U_ME",
            )

        assert sent is False


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


class TestPublicHelperSurface:
    """The three cross-package helpers are public names, not underscored privates.

    ``teatree.messaging.notify_with_fallback`` and the live-approval CLI
    reach for these helpers across the package boundary, so they are part
    of the module's public surface and must not carry a leading underscore.
    """

    def test_public_helpers_are_importable(self) -> None:
        assert callable(notify_module.format_notification)
        assert callable(notify_module.maybe_linkify)
        assert callable(notify_module.resolve_user_id)

    def test_old_private_names_are_gone(self) -> None:
        assert not hasattr(notify_module, "_format")
        assert not hasattr(notify_module, "_maybe_linkify")
        assert not hasattr(notify_module, "_resolve_user_id")

    def test_format_notification_prefixes_kind_marker(self) -> None:
        out = notify_module.format_notification("hello", NotifyKind.INFO)
        assert "hello" in out
        assert "info" in out.lower()
