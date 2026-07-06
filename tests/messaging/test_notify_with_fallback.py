"""Tests for the verified-delivery notify wrapper (#1181).

``notify_with_fallback`` tries the canonical :func:`teatree.core.notify.notify_user`
path first and, when that path does not deliver (the #1173 silent-rc=1
class), automatically falls back to a direct messaging-backend send, then
**round-trip verifies** delivery via ``fetch_message`` and records which
transport actually delivered on the :class:`BotPing` row.

Only the unstoppable Slack HTTP boundary (``messaging_from_overlay`` /
the backend's ``post_message`` / ``fetch_message``) is mocked — the rest
of the notify + audit path runs for real against the DB.
"""

from unittest.mock import MagicMock, patch

import pytest

from teatree.core.models import BotPing
from teatree.messaging.notify_with_fallback import NotifyTransport, notify_with_fallback

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_FALLBACK_TARGET = "teatree.messaging.notify_with_fallback.messaging_from_overlay"
_PRIMARY_TARGET = "teatree.messaging.notify_with_fallback.notify_user"


def _delivering_backend() -> MagicMock:
    """A backend whose direct send + round-trip read both succeed."""
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000100"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000100"
    b.fetch_message.return_value = {"ts": "1700000000.000100", "text": "the body"}
    return b


class TestPrimaryDelivers:
    def test_primary_success_skips_fallback_and_records_primary_transport(self) -> None:
        backend = _delivering_backend()
        with (
            patch(_PRIMARY_TARGET, return_value=True) as primary,
            patch(_FALLBACK_TARGET, return_value=backend) as fallback_backend,
        ):
            result = notify_with_fallback(
                "the body",
                kind="info",
                idempotency_key="k-primary-ok",
                user_id="U_ME",
            )

        assert result.delivered is True
        assert result.transport == NotifyTransport.PRIMARY
        primary.assert_called_once()
        # The fallback transport must never be touched when the primary delivers.
        fallback_backend.assert_not_called()


class TestFallbackFires:
    def test_primary_failure_triggers_verified_fallback(self) -> None:
        backend = _delivering_backend()
        with (
            patch(_PRIMARY_TARGET, return_value=False),
            patch(_FALLBACK_TARGET, return_value=backend),
        ):
            result = notify_with_fallback(
                "the body",
                kind="info",
                idempotency_key="k-fallback",
                user_id="U_ME",
            )

        assert result.delivered is True
        assert result.transport == NotifyTransport.FALLBACK
        # Fallback actually posted via the direct backend...
        backend.post_message.assert_called_once()
        posted_text = backend.post_message.call_args.kwargs["text"]
        assert "the body" in posted_text
        # ...and round-trip verified the landed message.
        backend.fetch_message.assert_called_once_with(channel="D-USER", ts="1700000000.000100")

        row = BotPing.objects.get(idempotency_key="k-fallback")
        assert row.status == BotPing.Status.SENT
        assert row.transport == NotifyTransport.FALLBACK.value
        assert row.posted_ts == "1700000000.000100"

    def test_fallback_records_primary_failure_for_observability(self) -> None:
        backend = _delivering_backend()
        with (
            patch(_PRIMARY_TARGET, return_value=False),
            patch(_FALLBACK_TARGET, return_value=backend),
        ):
            notify_with_fallback(
                "the body",
                kind="info",
                idempotency_key="k-observe",
                user_id="U_ME",
            )

        row = BotPing.objects.get(idempotency_key="k-observe")
        # The original primary failure is surfaced so #1173 stays diagnosable.
        assert "primary" in row.error_message.lower()


class TestFallbackVerificationGuards:
    def test_unverified_fallback_is_a_hard_failure(self) -> None:
        """A direct send whose round-trip read finds nothing is NOT delivered."""
        backend = _delivering_backend()
        backend.fetch_message.return_value = {}  # message never landed
        with (
            patch(_PRIMARY_TARGET, return_value=False),
            patch(_FALLBACK_TARGET, return_value=backend),
        ):
            result = notify_with_fallback(
                "the body",
                kind="info",
                idempotency_key="k-unverified",
                user_id="U_ME",
            )

        assert result.delivered is False
        assert result.transport == NotifyTransport.NONE
        row = BotPing.objects.get(idempotency_key="k-unverified")
        assert row.status == BotPing.Status.FAILED

    def test_fallback_post_failure_is_a_hard_failure(self) -> None:
        backend = _delivering_backend()
        backend.post_message.return_value = {"ok": False, "error": "channel_not_found"}
        with (
            patch(_PRIMARY_TARGET, return_value=False),
            patch(_FALLBACK_TARGET, return_value=backend),
        ):
            result = notify_with_fallback(
                "the body",
                kind="info",
                idempotency_key="k-postfail",
                user_id="U_ME",
            )

        assert result.delivered is False
        assert result.transport == NotifyTransport.NONE
        # A failed direct send must never be round-trip "verified".
        backend.fetch_message.assert_not_called()

    def test_no_backend_for_fallback_is_a_hard_failure(self) -> None:
        with (
            patch(_PRIMARY_TARGET, return_value=False),
            patch(_FALLBACK_TARGET, return_value=None),
        ):
            result = notify_with_fallback(
                "the body",
                kind="info",
                idempotency_key="k-nobackend",
                user_id="U_ME",
            )

        assert result.delivered is False
        assert result.transport == NotifyTransport.NONE


class TestNoopDoesNotFallBack:
    def test_primary_noop_skips_fallback(self) -> None:
        """A NOOP (nothing configured) is not a transport failure — no fallback.

        The real ``notify_user`` records a NOOP row and returns False when no
        backend / user_id is configured; a fallback over the same unconfigured
        transport cannot help, so the wrapper must not even reach for it.
        """
        backend = _delivering_backend()

        def _record_noop_and_fail(*_a: object, **kwargs: object) -> bool:
            BotPing.objects.create(
                idempotency_key=str(kwargs["idempotency_key"]),
                kind=BotPing.Kind.INFO,
                status=BotPing.Status.NOOP,
                text="the body",
            )
            return False

        with (
            patch(_PRIMARY_TARGET, side_effect=_record_noop_and_fail),
            patch(_FALLBACK_TARGET, return_value=backend) as fallback_backend,
        ):
            result = notify_with_fallback(
                "the body",
                kind="info",
                idempotency_key="k-noop",
                user_id="U_ME",
            )

        assert result.delivered is False
        assert result.transport == NotifyTransport.NONE
        # The fallback transport must never be reached for a NOOP.
        fallback_backend.assert_not_called()

    def test_primary_failed_transport_does_fall_back(self) -> None:
        """A FAILED transport error (the #1173 class) DOES trigger the fallback."""
        backend = _delivering_backend()

        def _record_failed_and_fail(*_a: object, **kwargs: object) -> bool:
            BotPing.objects.create(
                idempotency_key=str(kwargs["idempotency_key"]),
                kind=BotPing.Kind.INFO,
                status=BotPing.Status.FAILED,
                text="the body",
                error_message="Slack post failed: server_error",
            )
            return False

        with (
            patch(_PRIMARY_TARGET, side_effect=_record_failed_and_fail),
            patch(_FALLBACK_TARGET, return_value=backend),
        ):
            result = notify_with_fallback(
                "the body",
                kind="info",
                idempotency_key="k-failed-transport",
                user_id="U_ME",
            )

        assert result.delivered is True
        assert result.transport == NotifyTransport.FALLBACK
        backend.post_message.assert_called_once()
