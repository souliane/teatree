"""Backlog settlement for the stranded INFO DM accumulation (#2064)."""

import importlib
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import BotPing

migration_0058 = importlib.import_module(
    "teatree.core.migrations.0058_botping_attempts_alter_botping_status",
)


class _Apps:
    def get_model(self, app_label: str, model_name: str) -> type[BotPing]:
        assert (app_label, model_name) == ("core", "BotPing")
        return BotPing


class TestSettleStrandedInfoBacklog(TestCase):
    def _settle(self) -> None:
        migration_0058.settle_stranded_info_backlog(_Apps(), None)

    def test_aged_recoverable_info_is_expired(self) -> None:
        row = BotPing.objects.create(
            idempotency_key="historic-noop",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="from weeks ago",
        )
        BotPing.objects.filter(pk=row.pk).update(
            posted_at=timezone.now() - BotPing.REDELIVERY_AGE_CUTOFF - timedelta(hours=1),
        )
        self._settle()
        assert BotPing.objects.get(pk=row.pk).status == BotPing.Status.EXPIRED

    def test_fresh_recoverable_info_is_left_alone(self) -> None:
        BotPing.objects.create(
            idempotency_key="recent-noop",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="just stranded",
        )
        self._settle()
        assert [r.idempotency_key for r in BotPing.recoverable_info()] == ["recent-noop"]

    def test_sent_info_is_never_touched(self) -> None:
        row = BotPing.objects.create(
            idempotency_key="historic-sent",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.SENT,
            text="delivered long ago",
        )
        BotPing.objects.filter(pk=row.pk).update(
            posted_at=timezone.now() - BotPing.REDELIVERY_AGE_CUTOFF - timedelta(hours=1),
        )
        self._settle()
        assert BotPing.objects.get(pk=row.pk).status == BotPing.Status.SENT

    def test_settlement_is_idempotent(self) -> None:
        row = BotPing.objects.create(
            idempotency_key="historic-failed",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.FAILED,
            text="failed weeks ago",
        )
        BotPing.objects.filter(pk=row.pk).update(
            posted_at=timezone.now() - BotPing.REDELIVERY_AGE_CUTOFF - timedelta(hours=1),
        )
        self._settle()
        self._settle()
        assert BotPing.objects.get(pk=row.pk).status == BotPing.Status.EXPIRED
