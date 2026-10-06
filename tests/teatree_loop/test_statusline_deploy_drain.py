"""The loop line shows a deploy drain while it lasts, so a frozen queue is never silent (#5089)."""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.core.models import ConfigSetting
from teatree.loop.drain import QUIESCING_SETTING, set_worker_quiescing
from teatree.loop.statusline_loops import live_loops_anchor
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
