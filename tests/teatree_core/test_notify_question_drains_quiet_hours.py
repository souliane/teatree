"""No owner-question DM is sent 22:00-08:00 in the owner's zone; the first post held overnight goes out at 08:00.

The owner's zone is the active schedule's, else Europe/Paris — never ``settings.TIME_ZONE`` (UTC), which would
hold the window two hours off.
"""

import datetime as dt
import zoneinfo
from collections.abc import Callable
from contextlib import AbstractContextManager
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase, override_settings

from teatree.core import notify as notify_module
from teatree.core.models import ConfigSetting, DeferredQuestion, ModeSchedule
from teatree.core.notify_question_drains import (
    drain_deferred_questions,
    drain_unmirrored_deferred_questions,
    owner_quiet_now,
    reask_escalated_questions,
    resurface_question_backlog,
)
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from tests._owner_channel import OWNER_DECISION

_PARIS = zoneinfo.ZoneInfo("Europe/Paris")
_CHANNEL = "D0DEMOOWNER"
_DAYS = ("2026-10-12", "2026-03-29", "2026-10-25")

pytestmark = pytest.mark.real_quiet_hours


def _paris(day: str, hour: int, minute: int) -> dt.datetime:
    year, month, date = map(int, day.split("-"))
    return dt.datetime(year, month, date, hour, minute, tzinfo=_PARIS)


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = _CHANNEL
    backend.post_message.return_value = {"ok": True, "ts": "1800000000.000100"}
    backend.get_permalink.return_value = "https://acme.slack.example/archives/D0DEMOOWNER/p1800000000000100"
    return backend


def _at(moment: dt.datetime) -> AbstractContextManager[object]:
    return patch("django.utils.timezone.now", return_value=moment.astimezone(dt.UTC))


def _paris_schedule() -> None:
    ModeSchedule.objects.create(name="standard", timezone="Europe/Paris")
    ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")


def _mirrored(moment: dt.datetime) -> DeferredQuestion:
    posted_at = moment - dt.timedelta(hours=3)
    row = DeferredQuestion.record(
        "Can I ship the widgets?", slack_channel=_CHANNEL, slack_ts=f"{posted_at.timestamp():.6f}", **OWNER_DECISION
    )
    DeferredQuestion.objects.filter(pk=row.pk).update(created_at=posted_at)
    return row


def _every_send(moment: dt.datetime) -> list[Callable[[MagicMock], object]]:
    return [
        lambda backend: drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend),
        lambda backend: drain_deferred_questions(user_id="U0DEMOOWNER", backend=backend),
        lambda backend: resurface_question_backlog(user_id="U0DEMOOWNER", backend=backend, now=moment),
        lambda backend: reask_escalated_questions(user_id="U0DEMOOWNER", backend=backend, now=moment),
    ]


class _QuietHours(TestCase):
    def _assert_nothing_is_sent_at(self, moment: dt.datetime) -> None:
        DeferredQuestion.record("Can I archive the logs?", **OWNER_DECISION)
        _mirrored(moment)
        for send in _every_send(moment):
            backend = _backend()
            with _at(moment), patch.object(notify_module, "messaging_from_overlay", return_value=backend):
                send(backend)
            backend.post_message.assert_not_called()

    def _assert_the_first_post_goes_out_at(self, moment: dt.datetime) -> None:
        row = DeferredQuestion.record("Can I archive the logs?", **OWNER_DECISION)
        backend = _backend()
        with _at(moment), patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)
        backend.post_message.assert_called_once()
        row.refresh_from_db()
        assert row.slack_ts


class TestWithTheScheduleInParis(_QuietHours):
    def setUp(self) -> None:
        _paris_schedule()

    def test_nothing_is_sent_at_23_30_or_07_59_paris_time(self) -> None:
        for day in _DAYS:
            for hour, minute in ((23, 30), (7, 59), (22, 0), (3, 0)):
                with self.subTest(day=day, at=f"{hour}:{minute:02d}"):
                    DeferredQuestion.objects.all().delete()
                    self._assert_nothing_is_sent_at(_paris(day, hour, minute))

    def test_the_held_first_post_goes_out_at_08_00_paris_time(self) -> None:
        for day in _DAYS:
            with self.subTest(day=day):
                DeferredQuestion.objects.all().delete()
                self._assert_the_first_post_goes_out_at(_paris(day, 8, 0))

    def test_the_window_closes_at_22_00_not_21_59(self) -> None:
        for day in _DAYS:
            with self.subTest(day=day):
                DeferredQuestion.objects.all().delete()
                self._assert_the_first_post_goes_out_at(_paris(day, 21, 59))


@override_settings(TIME_ZONE="UTC")
class TestWithNoScheduleTheWindowIsStillParis(_QuietHours):
    def test_nothing_is_sent_at_23_30_or_07_59_paris_time(self) -> None:
        for day in _DAYS:
            for hour, minute in ((23, 30), (7, 59)):
                with self.subTest(day=day, at=f"{hour}:{minute:02d}"):
                    DeferredQuestion.objects.all().delete()
                    self._assert_nothing_is_sent_at(_paris(day, hour, minute))

    def test_the_held_first_post_goes_out_at_08_00_paris_time(self) -> None:
        for day in _DAYS:
            with self.subTest(day=day):
                DeferredQuestion.objects.all().delete()
                self._assert_the_first_post_goes_out_at(_paris(day, 8, 0))

    def test_utc_midnight_is_not_the_window(self) -> None:
        """23:30 UTC in summer is 01:30 in Paris (quiet), 06:30 UTC is 08:30 (open); a UTC window says the reverse."""
        with _at(dt.datetime(2026, 10, 12, 6, 30, tzinfo=dt.UTC)):
            assert owner_quiet_now() is False
        with _at(dt.datetime(2026, 10, 12, 23, 30, tzinfo=dt.UTC)):
            assert owner_quiet_now() is True


class TestAZoneTheScheduleNamesWins(TestCase):
    def test_a_schedule_in_another_zone_moves_the_window(self) -> None:
        ModeSchedule.objects.create(name="standard", timezone="America/New_York")
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")

        with _at(dt.datetime(2026, 10, 12, 5, 0, tzinfo=dt.UTC)):  # 01:00 New York, 07:00 Paris
            assert owner_quiet_now() is True
        with _at(dt.datetime(2026, 10, 12, 13, 0, tzinfo=dt.UTC)):  # 09:00 New York, 15:00 Paris
            assert owner_quiet_now() is False

    def test_an_invalid_schedule_zone_falls_back_to_paris_not_to_the_project_zone(self) -> None:
        ModeSchedule.objects.create(name="standard", timezone="Not/AZone")
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")

        with _at(_paris("2026-10-12", 23, 30)):
            assert owner_quiet_now() is True
