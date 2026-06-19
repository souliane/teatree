"""``build_report().mini_loops`` is cut over to the DB ``Loop`` table (#2513, D1).

After the cutover the live loop-status snapshot — the SINGLE source both the
statusline (`schedule.mini_loop_schedules`) and `t3 loop list` (`loop_list`) read —
derives its mini-loop rows from the ``Loop`` table (enabled / cadence / last_run /
next-due), NOT from ``LoopsConfig`` + ``MiniLoopMarker``. So a Loop row's enabled
flag, its delay/daily cadence, and its ``last_run_at`` drive the rendered status.
"""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.core.models import Loop, Prompt
from teatree.loops.live import LoopKind, build_report


def _prompt() -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name="demo-live", defaults={"body": "x"})
    return prompt


@django.test.override_settings(USE_TZ=True)
class TestMiniEntriesFromLoopTable(django.test.TestCase):
    def test_enabled_interval_loop_surfaced_from_row(self) -> None:
        Loop.objects.filter(name="lt-a").delete()
        now = timezone.now()
        Loop.objects.create(
            name="lt-a", delay_seconds=120, prompt=_prompt(), last_run_at=now - dt.timedelta(seconds=30)
        )
        entry = next(e for e in build_report(now=now).mini_loops if e.name == "lt-a")
        assert entry.kind is LoopKind.MINI
        assert entry.enabled is True
        assert entry.cadence_seconds == 120
        # last_run + delay = next_fire
        assert entry.next_fire_at == now - dt.timedelta(seconds=30) + dt.timedelta(seconds=120)

    def test_disabled_loop_row_renders_disabled(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="lt-off", delay_seconds=60, prompt=_prompt(), enabled=False)
        entry = next(e for e in build_report(now=now).mini_loops if e.name == "lt-off")
        assert entry.enabled is False

    def test_never_run_loop_has_no_next_fire(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="lt-new", delay_seconds=60, prompt=_prompt())
        entry = next(e for e in build_report(now=now).mini_loops if e.name == "lt-new")
        assert entry.last_fired_at is None
        assert entry.never_fired is True

    def test_daily_loop_cadence_reflects_day_seconds(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="lt-daily", delay_seconds=86400, daily_at=dt.time(8, 0), prompt=_prompt())
        entry = next(e for e in build_report(now=now).mini_loops if e.name == "lt-daily")
        # A daily loop is gated by its wall-clock schedule; cadence is the day window.
        assert entry.cadence_seconds == 86400

    def test_rows_sorted_by_name(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="lt-zzz", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="lt-aaa", delay_seconds=60, prompt=_prompt())
        names = [e.name for e in build_report(now=now).mini_loops if e.name.startswith("lt-")]
        assert names == sorted(names)
