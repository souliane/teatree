"""``UsageWindowState`` — the parked usage-window ledger (Directive #3, idle auto-recovery).

One active row per credential lane records that a usage window is exhausted and WHEN it
re-arms, so the recovery chain can clear it (and release parked tasks) deterministically at
the reset instant rather than dying silently.
"""

from datetime import timedelta

import django.test
from django.utils import timezone

from teatree.core.models import UsageWindowState
from teatree.core.models.task_attempt import TaskAttempt
from teatree.llm.anthropic_limits import LimitCause


class TestShouldClear(django.test.TestCase):
    """``resets_at`` is the effective re-arm instant; ``should_clear`` reads it deterministically."""

    def test_stored_reset_drives_should_clear(self) -> None:
        now = timezone.now()
        row = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=5),
            now=now,
        )
        assert row.should_clear(now) is False
        assert row.should_clear(now + timedelta(hours=4, minutes=59)) is False
        assert row.should_clear(now + timedelta(hours=5)) is True
        assert row.should_clear(now + timedelta(hours=6)) is True

    def test_null_reset_never_auto_clears(self) -> None:
        # A null reset (API-credit exhaustion — no time-based recovery) never clears on a timer.
        now = timezone.now()
        row = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.METERED,
            cause=LimitCause.API_CREDIT.value,
            resets_at=None,
            now=now,
        )
        assert row.resets_at is None
        assert row.should_clear(now + timedelta(days=30)) is False


class TestRecordLimitIdempotent(django.test.TestCase):
    """One active row per lane — a re-detection updates the existing row, never a duplicate."""

    def test_second_detection_same_lane_updates_not_duplicates(self) -> None:
        now = timezone.now()
        first = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=5),
            now=now,
        )
        second = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=4),
            now=now + timedelta(minutes=1),
        )
        assert second.pk == first.pk
        assert UsageWindowState.objects.filter(lane=TaskAttempt.Lane.SUBSCRIPTION).count() == 1
        second.refresh_from_db()
        assert second.resets_at == now + timedelta(hours=4)

    def test_distinct_lanes_get_distinct_rows(self) -> None:
        now = timezone.now()
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=5),
            now=now,
        )
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.METERED,
            cause=LimitCause.API_CREDIT.value,
            resets_at=None,
            now=now,
        )
        assert UsageWindowState.objects.active().count() == 2

    def test_cleared_row_does_not_block_a_fresh_detection(self) -> None:
        now = timezone.now()
        first = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=5),
            now=now,
        )
        first.clear(now + timedelta(hours=5))
        fresh = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=10),
            now=now + timedelta(hours=6),
        )
        assert fresh.pk != first.pk
        assert UsageWindowState.objects.active().count() == 1


class TestActiveForLane(django.test.TestCase):
    """The admission guard's query — an uncleared row covering the dispatch's lane."""

    def test_active_for_lane_matches_uncleared_only(self) -> None:
        now = timezone.now()
        row = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=5),
            now=now,
        )
        assert UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION) is not None
        assert UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.METERED) is None
        row.clear(now + timedelta(hours=5))
        assert UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION) is None

    def test_ambient_lane_is_the_empty_string_key(self) -> None:
        # The ambient-credential default resolves lane "" — record and match must agree on it.
        now = timezone.now()
        UsageWindowState.record_limit(
            lane="",
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=5),
            now=now,
        )
        assert UsageWindowState.objects.active_for_lane("") is not None
        assert UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.SUBSCRIPTION) is None


class TestStr(django.test.TestCase):
    def test_str_shows_lane_cause_and_state(self) -> None:
        now = timezone.now()
        row = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=5),
            now=now,
        )
        assert "usage-window" in str(row)
        assert "active" in str(row)
        row.clear(now + timedelta(hours=5))
        assert "cleared" in str(row)


class TestClear(django.test.TestCase):
    def test_clear_stamps_cleared_at(self) -> None:
        now = timezone.now()
        row = UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            cause=LimitCause.SUBSCRIPTION_SESSION.value,
            resets_at=now + timedelta(hours=5),
            now=now,
        )
        cleared_at = now + timedelta(hours=5, minutes=1)
        row.clear(cleared_at)
        row.refresh_from_db()
        assert row.cleared_at == cleared_at
        assert row not in UsageWindowState.objects.active()
