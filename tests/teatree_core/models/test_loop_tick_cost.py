"""``Loop.idle_cpu_seconds`` / ``busy_cpu_seconds`` — the per-loop tick CPU EWMA (#4999)."""

import datetime as dt

import django.test
import pytest

from teatree.core.models import Loop


def _loop(name: str = "probe", *, delay_seconds: int | None = 60, **extra: object) -> Loop:
    return Loop.objects.create(
        name=name, script=f"src/teatree/loops/{name}/loop.py", delay_seconds=delay_seconds, **extra
    )


class TestRecordTickCost(django.test.TestCase):
    def test_first_sample_is_taken_as_is(self) -> None:
        loop = _loop()

        Loop.objects.record_tick_cost("probe", cpu_seconds=6.0, found_work=False)

        loop.refresh_from_db()
        assert loop.idle_cpu_seconds == pytest.approx(6.0)
        assert loop.busy_cpu_seconds is None

    def test_later_samples_blend_at_one_fifth(self) -> None:
        loop = _loop()
        Loop.objects.record_tick_cost("probe", cpu_seconds=10.0, found_work=False)

        Loop.objects.record_tick_cost("probe", cpu_seconds=5.0, found_work=False)

        loop.refresh_from_db()
        assert loop.idle_cpu_seconds == pytest.approx(9.0)

    def test_busy_and_idle_samples_land_in_their_own_column(self) -> None:
        loop = _loop()

        Loop.objects.record_tick_cost("probe", cpu_seconds=4.0, found_work=False)
        Loop.objects.record_tick_cost("probe", cpu_seconds=20.0, found_work=True)

        loop.refresh_from_db()
        assert (loop.idle_cpu_seconds, loop.busy_cpu_seconds) == (4.0, 20.0)

    def test_an_unknown_name_updates_no_row(self) -> None:
        assert Loop.objects.record_tick_cost("ghost", cpu_seconds=1.0, found_work=False) == 0

    def test_recording_leaves_the_config_edit_stamp_alone(self) -> None:
        loop = _loop()
        stamped = loop.updated_at

        Loop.objects.record_tick_cost("probe", cpu_seconds=1.0, found_work=False)

        loop.refresh_from_db()
        assert loop.updated_at == stamped


class TestIdleCoreShare(django.test.TestCase):
    def test_is_the_idle_cost_over_the_cadence(self) -> None:
        loop = _loop(delay_seconds=60, idle_cpu_seconds=6.0)

        assert loop.idle_core_share == pytest.approx(0.1)

    def test_a_daily_loop_scales_to_the_day(self) -> None:
        loop = _loop(delay_seconds=300, daily_at=dt.time(8, 0), idle_cpu_seconds=8640.0)

        assert loop.idle_core_share == pytest.approx(0.1)

    def test_off_live_ticks_scale_to_the_driver_wake_interval(self) -> None:
        for name, delay, daily_at in (
            ("dream", 86400, dt.time(8, 0)),
            ("outer_loop", 3600, None),
            ("directive_loop", 3600, None),
        ):
            with self.subTest(loop=name):
                loop = Loop(name=name, delay_seconds=delay, daily_at=daily_at, idle_cpu_seconds=6.0)
                assert loop.idle_core_share == pytest.approx(0.01)

    def test_is_none_without_a_sample(self) -> None:
        assert _loop().idle_core_share is None

    def test_is_none_for_an_every_tick_loop(self) -> None:
        assert Loop(name="probe", delay_seconds=None, idle_cpu_seconds=3.0).idle_core_share is None
