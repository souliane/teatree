"""``notify_user`` → ``PendingChatInjection.answered_at`` integration (#1063).

When the agent posts a Slack DM that is a *reply* to a queued user
question, ``notify_user`` auto-stamps ``answered_at`` on the matching
:class:`PendingChatInjection` row so the Stop-hook gate stops nagging.

Two trigger forms — both exercised here: the explicit
``answering_slack_ts=`` kwarg (canonical, programmatic), and the
idempotency-key pattern ``answer-<anything>-<slack_ts>`` (convention
for callers that don't plumb the kwarg through).

Stub Slack backend records calls; the real ``notify_user`` flow runs,
including the real :class:`BotPing` audit and the real
``PendingChatInjection`` row stamping.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import PendingChatInjection
from teatree.core.notify import NotifyKind, notify_user


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "9999999999.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/x/p9999"
    return b


class TestExplicitAnsweringSlackTs(TestCase):
    """Passing ``answering_slack_ts=`` stamps the matching row."""

    def test_kwarg_stamps_answered_at(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0001", text="why?")

        sent = notify_user(
            "Because we removed the legacy test runner; details in PR #123.",
            kind=NotifyKind.ANSWER,
            idempotency_key="explicit-kwarg-A",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
            answering_slack_ts="1700000000.0001",
        )

        assert sent is True
        row = PendingChatInjection.objects.get(slack_ts="1700000000.0001")
        assert row.answered_at is not None

    def test_kwarg_does_not_stamp_other_rows(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0001", text="why?")
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0002", text="when?")

        notify_user(
            "explanation",
            kind=NotifyKind.ANSWER,
            idempotency_key="explicit-B",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
            answering_slack_ts="1700000000.0001",
        )

        assert PendingChatInjection.objects.get(slack_ts="1700000000.0001").answered_at is not None
        assert PendingChatInjection.objects.get(slack_ts="1700000000.0002").answered_at is None

    def test_kwarg_empty_string_is_no_op(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0001", text="why?")

        notify_user(
            "ack",
            kind=NotifyKind.INFO,
            idempotency_key="empty-ts",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
            answering_slack_ts="",
        )

        assert PendingChatInjection.objects.get().answered_at is None

    @patch.dict("os.environ", {"T3_OVERLAY_NAME": "overlay-beta"})
    def test_stamps_row_recorded_under_a_different_overlay(self) -> None:
        """Sub-case (b): a reply from one overlay clears a question recorded by another.

        In a concurrent multi-overlay deployment the answering session's
        ``T3_OVERLAY_NAME`` routinely differs from the overlay that recorded
        the question. The earlier scoped stamp forwarded ``T3_OVERLAY_NAME``
        into an exact-overlay filter and stamped 0 rows, so ``answered_at``
        stayed NULL and the unscoped Stop-hook gate nagged forever. The
        ts-keyed stamp clears the row regardless of the answering overlay.
        """
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0001", text="why?", overlay="overlay-alpha")

        sent = notify_user(
            "Because the migration ran out of order.",
            kind=NotifyKind.ANSWER,
            idempotency_key="cross-overlay-answer",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
            answering_slack_ts="1700000000.0001",
        )

        assert sent is True
        assert PendingChatInjection.objects.get().answered_at is not None


class TestIdempotencyKeyPattern(TestCase):
    """``answer-<anything>-<slack_ts>`` key auto-stamps without explicit kwarg."""

    def test_pattern_match_stamps(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0001", text="why?")

        notify_user(
            "Because…",
            kind=NotifyKind.ANSWER,
            idempotency_key="answer-q1-1700000000.0001",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
        )

        assert PendingChatInjection.objects.get().answered_at is not None

    def test_pattern_match_with_complex_middle_part(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0001", text="why?")

        notify_user(
            "Because…",
            kind=NotifyKind.ANSWER,
            idempotency_key="answer-session=abc;turn=12;q=why-1700000000.0001",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
        )

        assert PendingChatInjection.objects.get().answered_at is not None

    def test_non_matching_key_does_not_stamp(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0001", text="why?")

        notify_user(
            "unrelated info",
            kind=NotifyKind.INFO,
            idempotency_key="info-something-unrelated",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
        )

        assert PendingChatInjection.objects.get().answered_at is None

    def test_explicit_kwarg_wins_over_pattern(self) -> None:
        """If both are present, the explicit kwarg's ts is the one stamped."""
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0001", text="why?")
        PendingChatInjection.record(channel="D", slack_ts="2700000000.0002", text="what?")

        notify_user(
            "Because…",
            kind=NotifyKind.ANSWER,
            # Key has a different ts to the kwarg.
            idempotency_key="answer-q-2700000000.0002",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
            answering_slack_ts="1700000000.0001",
        )

        assert PendingChatInjection.objects.get(slack_ts="1700000000.0001").answered_at is not None
        assert PendingChatInjection.objects.get(slack_ts="2700000000.0002").answered_at is None


class TestIdempotency(TestCase):
    """A second call (same key) is a duplicate; the answer-stamp must remain stable."""

    def test_duplicate_call_keeps_first_stamp(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0001", text="why?")

        notify_user(
            "first reply",
            kind=NotifyKind.ANSWER,
            idempotency_key="dup-key",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
            answering_slack_ts="1700000000.0001",
        )
        first_stamp = PendingChatInjection.objects.get().answered_at

        notify_user(
            "second reply",
            kind=NotifyKind.ANSWER,
            idempotency_key="dup-key",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
            answering_slack_ts="1700000000.0001",
        )

        # Duplicate idempotency-key short-circuits before the stamp; the
        # first stamp from the first call is preserved.
        row = PendingChatInjection.objects.get()
        assert row.answered_at == first_stamp


class TestNoMatchingRow(TestCase):
    def test_unknown_slack_ts_is_a_silent_no_op(self) -> None:
        """notify_user must succeed even if no row matches the ts."""
        sent = notify_user(
            "ack to nothing",
            kind=NotifyKind.ANSWER,
            idempotency_key="answer-x-9999999999.9999",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=_backend(),
            user_id="U_ME",
        )

        assert sent is True
        assert PendingChatInjection.objects.count() == 0


class TestStampFailureIsBestEffort(TestCase):
    """A DB error during the stamp must not break ``notify_user``."""

    def test_stamp_exception_is_swallowed(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700000000.0001", text="why?")

        with patch.object(
            PendingChatInjection,
            "agent_answered_question",
            side_effect=RuntimeError("db exploded"),
        ):
            sent = notify_user(
                "Because…",
                kind=NotifyKind.ANSWER,
                idempotency_key="best-effort-key",
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=_backend(),
                user_id="U_ME",
                answering_slack_ts="1700000000.0001",
            )

        # The transport succeeded and the audit row exists; the stamp
        # failure was logged-and-swallowed, never re-raised.
        assert sent is True
        assert PendingChatInjection.objects.get().answered_at is None
