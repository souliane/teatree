"""Notification-relevance policy — deny-by-default owner-DM audience gating.

The owner reads only the four owner audiences; everything INTERNAL is logged and
terminally recorded but never DM'd, and never re-delivered by the cross-tick drain.
"""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import BotPing
from teatree.core.notify import NotifyKind, drain_undelivered_notifies, notify_user


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


class TestInternalAudienceIsLogOnly(TestCase):
    def test_internal_posts_nothing_and_records_a_logged_row(self) -> None:
        backend = _backend()
        sent = notify_user(
            "flag (no-review) souliane/teatree#3280",
            kind=NotifyKind.INFO,
            idempotency_key="pr-sweep-flag:souliane/teatree#3280:no-review",
            audience=NotifyAudience.INTERNAL,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is False
        backend.open_dm.assert_not_called()
        backend.post_message.assert_not_called()
        row = BotPing.objects.get(idempotency_key="pr-sweep-flag:souliane/teatree#3280:no-review")
        assert row.status == BotPing.Status.LOGGED
        assert row.audience == NotifyAudience.INTERNAL.value

    def test_internal_short_circuits_even_with_no_backend(self) -> None:
        sent = notify_user(
            "internal signal",
            kind=NotifyKind.INFO,
            idempotency_key="internal-no-backend",
            audience=NotifyAudience.INTERNAL,
        )
        assert sent is False
        assert BotPing.objects.get(idempotency_key="internal-no-backend").status == BotPing.Status.LOGGED

    def test_repeat_internal_flag_does_not_accumulate_rows(self) -> None:
        for _ in range(3):
            notify_user(
                "flag (no-review) souliane/teatree#3280",
                kind=NotifyKind.INFO,
                idempotency_key="pr-sweep-flag:souliane/teatree#3280:no-review",
                audience=NotifyAudience.INTERNAL,
            )
        assert BotPing.objects.filter(idempotency_key="pr-sweep-flag:souliane/teatree#3280:no-review").count() == 1

    def test_internal_row_is_excluded_from_recoverable_and_expired(self) -> None:
        BotPing.objects.create(
            idempotency_key="internal-noop",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="internal signal",
            audience=NotifyAudience.INTERNAL.value,
        )
        assert list(BotPing.recoverable_info()) == []
        BotPing.expire_stale_info()
        assert BotPing.objects.get(idempotency_key="internal-noop").status == BotPing.Status.EXPIRED

    def test_pre_migration_blank_audience_row_is_excluded_and_expired(self) -> None:
        BotPing.objects.create(
            idempotency_key="pre-migration",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="stale operator noise",
            audience="",
        )
        assert list(BotPing.recoverable_info()) == []
        BotPing.expire_stale_info()
        assert BotPing.objects.get(idempotency_key="pre-migration").status == BotPing.Status.EXPIRED


class TestOwnerAudienceDelivers(TestCase):
    def test_owner_delivery_posts_and_is_recoverable_when_stranded(self) -> None:
        backend = _backend()
        sent = notify_user(
            "merged souliane/teatree#3280 @ deadbeef",
            kind=NotifyKind.INFO,
            idempotency_key="merge-announce:souliane/teatree#3280:deadbeef",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=backend,
            user_id="U_ME",
        )
        assert sent is True
        backend.post_message.assert_called_once()
        row = BotPing.objects.get(idempotency_key="merge-announce:souliane/teatree#3280:deadbeef")
        assert row.status == BotPing.Status.SENT
        assert row.audience == NotifyAudience.OWNER_DELIVERY.value

    def test_stranded_owner_row_redelivers_when_backend_available(self) -> None:
        BotPing.objects.create(
            idempotency_key="stranded-owner",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="merged souliane/teatree#1 @ abc",
            audience=NotifyAudience.OWNER_DELIVERY.value,
        )
        delivered, total = drain_undelivered_notifies(user_id="U_ME", backend=_backend())
        assert (delivered, total) == (1, 1)
