"""``Loop`` liveness vs cadence — the two anchors #4355 split apart.

``last_run_at`` is the CADENCE anchor a pass may deliberately withhold (``dream``
retries until a pass stamps success, #2285). Every health reader treats it as the
LIVENESS anchor — "did this loop run" — so a loop driven every 10 minutes rendered
as frozen for 6.6 days. ``last_attempt_at`` is the half no pass may withhold.
"""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Loop, Prompt


def _loop(**overrides: object) -> Loop:
    defaults: dict[str, object] = {
        "name": "probe",
        "script": "src/teatree/loops/probe/loop.py",
        "delay_seconds": 60,
    }
    return Loop.objects.create(**{**defaults, **overrides})


def _prompt_loop(**overrides: object) -> Loop:
    """A prompt-backed row — the only shape the ``loop_script_requires_delay`` constraint lets go interval-less."""
    prompt = Prompt.objects.create(name="probe-prompt", body="tick")
    return _loop(script="", prompt=prompt, delay_seconds=None, **overrides)


class AttemptAnchorTestCase(TestCase):
    def test_mark_attempted_moves_liveness_and_leaves_the_cadence_anchor(self) -> None:
        _loop()
        now = timezone.now()
        Loop.objects.mark_attempted("probe", now)
        row = Loop.objects.get(name="probe")
        assert row.last_attempt_at == now
        assert row.last_run_at is None

    def test_mark_run_moves_both_anchors(self) -> None:
        _loop()
        now = timezone.now()
        Loop.objects.mark_run("probe", now)
        row = Loop.objects.get(name="probe")
        assert row.last_run_at == now
        assert row.last_attempt_at == now

    def test_the_cas_bump_moves_both_anchors(self) -> None:
        _loop()
        now = timezone.now()
        assert Loop.objects.mark_run_if_unchanged("probe", previous_last_run_at=None, now=now) is True
        row = Loop.objects.get(name="probe")
        assert row.last_run_at == now
        assert row.last_attempt_at == now

    def test_a_lost_cas_moves_neither_anchor(self) -> None:
        ran = timezone.now() - dt.timedelta(hours=1)
        _loop(last_run_at=ran)
        assert Loop.objects.mark_run_if_unchanged("probe", previous_last_run_at=None, now=timezone.now()) is False
        row = Loop.objects.get(name="probe")
        assert row.last_run_at == ran
        assert row.last_attempt_at is None


class CadenceSecondsTestCase(TestCase):
    def test_an_interval_loop_reports_its_interval(self) -> None:
        assert _loop(delay_seconds=604800).cadence_seconds == 604800

    def test_a_daily_slot_reports_a_day(self) -> None:
        assert _prompt_loop(daily_at=dt.time(3, 0)).cadence_seconds == 86400

    def test_a_daily_slot_wins_over_a_stored_interval(self) -> None:
        # The same precedence ``cadence_label`` renders — the slot decides when it fires,
        # and every shipped daily row (``dream``, ``news``) carries both columns.
        assert _loop(delay_seconds=60, daily_at=dt.time(3, 0)).cadence_seconds == 86400

    def test_an_every_tick_loop_declares_no_cadence(self) -> None:
        assert _prompt_loop().cadence_seconds is None
