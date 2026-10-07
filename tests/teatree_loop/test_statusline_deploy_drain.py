"""The loop line shows a deploy drain while it lasts, so a frozen queue is never silent (#5089)."""

import datetime as dt
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import ConfigSetting
from teatree.loop.drain import QUIESCING_SETTING, set_worker_quiescing
from teatree.loop.statusline_loops import config_tier_chip, live_loops_anchor
from tests.factories import TaskFactory


class TestTheDeployDrainChip(django.test.TestCase):
    def test_a_quiesced_worker_shows_how_long_and_how_many_runs_are_in_flight(self) -> None:
        set_worker_quiescing(value=True)
        ConfigSetting.objects.filter(key=QUIESCING_SETTING).update(updated_at=timezone.now() - dt.timedelta(minutes=4))
        TaskFactory().claim(claimed_by="worker-A", lease_seconds=900)

        (line,) = live_loops_anchor()

        assert "deploy drain 4m, 1 in flight" in line

    def test_an_open_gate_shows_no_chip(self) -> None:
        set_worker_quiescing(value=False)
        TaskFactory().claim(claimed_by="worker-A", lease_seconds=900)

        assert not any("deploy drain" in line for line in live_loops_anchor())


def test_an_unreachable_db_drops_only_the_chip_and_never_marks_the_config_tier() -> None:
    # No DB access here, so the chip's ORM read genuinely fails, as on a box whose control DB is unreachable.
    lease = [("loop-tick", dt.datetime.now(dt.UTC) - dt.timedelta(seconds=60))]
    with (
        patch("teatree.loop.statusline_loops._live_loop_leases", return_value=lease),
        patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
    ):
        (line,) = live_loops_anchor()

    assert "tick 11m" in line
    assert "deploy drain" not in line
    assert config_tier_chip() == [], "the chip's failed read must not report the whole config tier unreadable"
