"""DB-configured autonomous loop model (#1796).

Each loop is its own autonomous row with its own cadence — a fixed
``delay_seconds`` interval or a ``daily_at`` once-per-day wall-clock time. These
tests pin the cadence gate (interval + daily), the manager surface, and the
one-time seed of the autonomous loop set. Integration-first against the real DB;
``demo-*`` names never collide with the seeded production loop names.
"""

import datetime as dt

from django.test import TestCase, override_settings
from django.utils import timezone

from teatree.core.models import Loop


class TestLoopDefaults(TestCase):
    def test_prompt_optional_enabled_default_last_run_absent(self) -> None:
        loop = Loop.objects.create(name="demo-x", delay_seconds=300)
        assert loop.prompt == ""
        assert loop.enabled is True
        assert loop.last_run_at is None
        assert loop.daily_at is None

    def test_str_describes_name_state_and_cadence(self) -> None:
        loop = Loop.objects.create(name="demo-ship", delay_seconds=300)
        rendered = str(loop)
        assert "demo-ship" in rendered
        assert "enabled" in rendered
        assert "every 300s" in rendered


class TestLoopIntervalCadence(TestCase):
    def test_never_run_loop_is_due_no_age_no_next(self) -> None:
        now = timezone.now()
        loop = Loop.objects.create(name="demo-new", delay_seconds=300)
        assert loop.seconds_since_run(now) is None
        assert loop.is_due(now) is True
        assert loop.next_run_at() is None
        assert loop.cadence_label == "every 300s"

    def test_recently_run_not_due_until_delay_elapses(self) -> None:
        now = timezone.now()
        loop = Loop.objects.create(name="demo-fresh", delay_seconds=300, last_run_at=now - dt.timedelta(seconds=120))
        assert loop.is_due(now) is False
        loop.last_run_at = now - dt.timedelta(seconds=301)
        assert loop.is_due(now) is True

    def test_next_run_at_is_last_plus_delay(self) -> None:
        now = timezone.now()
        loop = Loop.objects.create(name="demo-next", delay_seconds=300, last_run_at=now)
        assert loop.next_run_at() == now + dt.timedelta(seconds=300)


@override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopDailyCadence(TestCase):
    def _at(self, hour: int, minute: int = 0) -> dt.datetime:
        return dt.datetime(2026, 6, 16, hour, minute, tzinfo=dt.UTC)

    def test_cadence_label_shows_daily_time(self) -> None:
        loop = Loop.objects.create(name="demo-daily", delay_seconds=86400, daily_at=dt.time(8, 0))
        assert loop.cadence_label == "daily 08:00"

    def test_never_run_not_due_before_scheduled_time(self) -> None:
        loop = Loop.objects.create(name="demo-daily", delay_seconds=86400, daily_at=dt.time(8, 0))
        assert loop.is_due(self._at(7)) is False

    def test_never_run_due_after_scheduled_time(self) -> None:
        loop = Loop.objects.create(name="demo-daily", delay_seconds=86400, daily_at=dt.time(8, 0))
        assert loop.is_due(self._at(9)) is True

    def test_not_due_again_after_running_today(self) -> None:
        loop = Loop.objects.create(
            name="demo-daily", delay_seconds=86400, daily_at=dt.time(8, 0), last_run_at=self._at(8, 1)
        )
        assert loop.is_due(self._at(9)) is False

    def test_due_next_day_after_scheduled_time(self) -> None:
        loop = Loop.objects.create(
            name="demo-daily", delay_seconds=86400, daily_at=dt.time(8, 0), last_run_at=self._at(8, 1)
        )
        tomorrow_9 = (self._at(8, 1) + dt.timedelta(days=1)).replace(hour=9, minute=0)
        assert loop.is_due(tomorrow_9) is True

    def test_next_run_at_returns_a_datetime(self) -> None:
        loop = Loop.objects.create(name="demo-daily", delay_seconds=86400, daily_at=dt.time(8, 0))
        assert loop.next_run_at() is not None


class TestLoopManager(TestCase):
    def test_enabled_excludes_disabled(self) -> None:
        Loop.objects.create(name="demo-on", delay_seconds=60)
        Loop.objects.create(name="demo-disabled", delay_seconds=60, enabled=False)
        names = {row.name for row in Loop.objects.enabled()}
        assert "demo-on" in names
        assert "demo-disabled" not in names

    def test_due_returns_enabled_overdue_only(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="demo-due", delay_seconds=60)
        Loop.objects.create(name="demo-cooling", delay_seconds=60, last_run_at=now)
        Loop.objects.create(name="demo-due-off", delay_seconds=60, enabled=False)
        due = {row.name for row in Loop.objects.due(now)}
        assert "demo-due" in due
        assert "demo-cooling" not in due
        assert "demo-due-off" not in due

    def test_mark_run_sets_last_run_at(self) -> None:
        Loop.objects.create(name="demo-mark", delay_seconds=60)
        ts = timezone.now()
        Loop.objects.mark_run("demo-mark", ts)
        assert Loop.objects.get(name="demo-mark").last_run_at == ts


class TestLoopSeed(TestCase):
    """The data migration seeds one autonomous row per loop (#1796)."""

    def test_interval_loops_seeded_with_their_cadence(self) -> None:
        assert Loop.objects.get(name="inbox").delay_seconds == 60
        assert Loop.objects.get(name="audit").delay_seconds == 1800
        assert Loop.objects.get(name="followup").delay_seconds == 1800
        assert Loop.objects.get(name="arch_review").delay_seconds == 10800
        assert Loop.objects.get(name="slack_answer").delay_seconds == 20

    def test_daily_loops_seeded_with_schedule(self) -> None:
        assert Loop.objects.get(name="news").daily_at == dt.time(8, 0)
        assert Loop.objects.get(name="dream").daily_at == dt.time(3, 0)
        assert Loop.objects.get(name="dogfood").delay_seconds == 86400

    def test_eval_local_seeded_enabled_daily(self) -> None:
        loop = Loop.objects.get(name="eval_local")
        assert loop.enabled is True
        assert loop.delay_seconds == 86400

    def test_every_loop_is_its_own_autonomous_row(self) -> None:
        assert Loop.objects.count() == 19
        assert Loop.objects.filter(name="dispatch").exists()
