"""Worker-driven off-live ticks leave a cost sample for each loop."""

import os
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands.dream import PassOutcome
from teatree.core.models import Loop, LoopLease
from teatree.loops.deadlined_tick import TICK_SUBPROCESS_ENV_MARKER
from teatree.loops.directive_loop.tick import DirectiveTickResult
from teatree.loops.outer_loop.tick import OuterLoopTickResult
from teatree.loops.seed import seed_default_loops_and_prompts


class TestOffLiveTickCost(TestCase):
    def test_worker_records_idle_cost_for_each_skipped_loop(self) -> None:
        seed_default_loops_and_prompts()
        commands = {"dream": "dream", "outer_loop": "outer", "directive_loop": "directive"}
        Loop.objects.filter(name__in=commands).update(enabled=False)

        with patch.dict(os.environ, {TICK_SUBPROCESS_ENV_MARKER: "1"}):
            for name, command in commands.items():
                with self.subTest(loop=name):
                    call_command(command, "tick", stdout=StringIO())
                    loop = Loop.objects.get(name=name)
                    assert loop.idle_cpu_seconds is not None
                    assert loop.busy_cpu_seconds is None

    def test_worker_records_busy_cost_for_each_executed_loop(self) -> None:
        seed_default_loops_and_prompts()
        names = ("dream", "outer_loop", "directive_loop")
        Loop.objects.filter(name__in=names).update(enabled=True, last_run_at=None)

        with (
            patch.dict(os.environ, {TICK_SUBPROCESS_ENV_MARKER: "1"}),
            patch("teatree.core.management.commands.dream.Command._run_pass", return_value=PassOutcome.STAMPED),
            patch("teatree.loops.enable_verdict.loop_admits", return_value=True),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.loops.outer_loop.tick.run_tick", return_value=OuterLoopTickResult(action="proposed")),
            patch(
                "teatree.loops.directive_loop.tick.run_tick",
                return_value=DirectiveTickResult(action="ratify_asked"),
            ),
        ):
            for name, command in (("dream", "dream"), ("outer_loop", "outer"), ("directive_loop", "directive")):
                with self.subTest(loop=name):
                    call_command(command, "tick", stdout=StringIO())
                    loop = Loop.objects.get(name=name)
                    assert loop.busy_cpu_seconds is not None
                    assert loop.idle_cpu_seconds is None
