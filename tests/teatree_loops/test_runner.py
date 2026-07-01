"""teatree.loops.runner — the self-owned singleton loop-runner daemon (#2876).

Integration-first against the real DB for the beat / cadence, and injected
collaborators for the supervision loop (no real clock, no real queue). ``iter_loops``
is patched to a small stub set so the beat's verdict does not depend on the seeded
production loops.
"""

import datetime as dt
from unittest.mock import patch

import django.test
import pytest
from django.utils import timezone

from teatree.core.models import Loop, LoopState, Prompt
from teatree.loops.base import MiniLoop
from teatree.loops.runner import LoopRunnerDaemon, compute_beat_seconds, enqueue_due_loops


def _mini(name: str) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=lambda n=name, **_: [f"job-{n}"])


def _prompt(name: str = "demo-prompt") -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": "do x"})
    return prompt


@django.test.override_settings(USE_TZ=True)
class TestComputeBeatSeconds(django.test.TestCase):
    """Decision-1 beat clamp: ``max(5, min(30, min_enabled_delay / 2))``, daily excluded."""

    def setUp(self) -> None:
        Loop.objects.all().delete()  # ignore any migration-seeded rows so the clamp is deterministic

    def test_no_interval_loop_sits_at_ceiling(self) -> None:
        Loop.objects.create(name="cb-daily", daily_at=dt.time(8, 0), prompt=_prompt())
        assert compute_beat_seconds() == pytest.approx(30.0)

    def test_half_shortest_enabled_interval(self) -> None:
        Loop.objects.create(name="cb-20", delay_seconds=20, prompt=_prompt())
        Loop.objects.create(name="cb-60", delay_seconds=60, prompt=_prompt())
        assert compute_beat_seconds() == pytest.approx(10.0)

    def test_ceiling_clamps_slow_loops(self) -> None:
        Loop.objects.create(name="cb-600", delay_seconds=600, prompt=_prompt())
        assert compute_beat_seconds() == pytest.approx(30.0)

    def test_floor_clamps_fast_loops(self) -> None:
        Loop.objects.create(name="cb-8", delay_seconds=8, prompt=_prompt())
        assert compute_beat_seconds() == pytest.approx(5.0)

    def test_daily_only_loop_does_not_lower_beat(self) -> None:
        # daily_at set -> excluded from the interval min even with a delay_seconds present.
        Loop.objects.create(name="cb-daily2", daily_at=dt.time(8, 0), delay_seconds=10, prompt=_prompt())
        assert compute_beat_seconds() == pytest.approx(30.0)

    def test_disabled_interval_loop_is_ignored(self) -> None:
        Loop.objects.create(name="cb-dis", delay_seconds=10, prompt=_prompt(), enabled=False)
        assert compute_beat_seconds() == pytest.approx(30.0)


@django.test.override_settings(USE_TZ=True)
class TestEnqueueDueLoops(django.test.TestCase):
    """The beat body: one ``execute_loop`` task per admitted row, nothing when silent."""

    def test_enqueues_exactly_one_task_per_admitted_row(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="be-a", delay_seconds=60, prompt=_prompt())  # due
        Loop.objects.create(name="be-b", delay_seconds=60, prompt=_prompt())  # due
        Loop.objects.create(name="be-cool", delay_seconds=60, prompt=_prompt(), last_run_at=now)  # not due
        Loop.objects.create(name="be-off", delay_seconds=60, prompt=_prompt(), enabled=False)  # disabled
        registry = (_mini("be-a"), _mini("be-b"), _mini("be-cool"), _mini("be-off"))
        with (
            patch("teatree.loops.loop_table.iter_loops", return_value=registry),
            patch("teatree.core.tasks.execute_loop") as mock_loop,
        ):
            names = enqueue_due_loops(now=now)
        assert sorted(names) == ["be-a", "be-b"]
        assert mock_loop.enqueue.call_count == 2
        assert {call.args[0] for call in mock_loop.enqueue.call_args_list} == {"be-a", "be-b"}

    def test_paused_and_cooling_rows_are_skipped(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="be-paused", delay_seconds=60, prompt=_prompt())  # enabled + due but held
        Loop.objects.create(name="be-cooling", delay_seconds=60, prompt=_prompt(), last_run_at=now)  # not due
        LoopState.objects.pause("be-paused")
        registry = (_mini("be-paused"), _mini("be-cooling"))
        with (
            patch("teatree.loops.loop_table.iter_loops", return_value=registry),
            patch("teatree.core.tasks.execute_loop") as mock_loop,
        ):
            names = enqueue_due_loops(now=now)
        assert names == []
        mock_loop.enqueue.assert_not_called()

    def test_silent_beat_enqueues_nothing_so_no_model_is_dispatched(self) -> None:
        # No admitted row -> zero enqueue -> zero downstream per-loop tick -> zero
        # model call. The silent tick stays zero-cost at the driver layer.
        now = timezone.now()
        Loop.objects.create(name="be-none", delay_seconds=60, prompt=_prompt(), last_run_at=now)  # not due
        with (
            patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("be-none"),)),
            patch("teatree.core.tasks.execute_loop") as mock_loop,
        ):
            names = enqueue_due_loops(now=now)
        assert names == []
        mock_loop.enqueue.assert_not_called()


class TestLoopRunnerDaemonSupervision(django.test.SimpleTestCase):
    """The supervisor respawns a crashed beat worker; ``run_once`` beats then drains."""

    def test_supervisor_respawns_a_crashed_beat_worker(self) -> None:
        calls = {"n": 0}

        def flaky_beat() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                msg = "boom"
                raise RuntimeError(msg)

        daemon = LoopRunnerDaemon(
            beat=flaky_beat,
            drain=lambda: None,
            beat_seconds=lambda: 0.0,
            sleep=lambda _s: None,
            stop=lambda: calls["n"] >= 2,
        )
        with self.assertLogs("teatree.loops.runner", level="ERROR") as logs:
            daemon.run()
        assert calls["n"] == 2  # crashed on the 1st beat, respawned, ran the 2nd
        assert any("respawn" in line.lower() for line in logs.output)

    def test_run_once_beats_then_drains_once(self) -> None:
        order: list[str] = []
        daemon = LoopRunnerDaemon(
            beat=lambda: order.append("beat"),
            drain=lambda: order.append("drain"),
            sleep=lambda _s: None,
            stop=lambda: False,
        )
        daemon.run_once()
        assert order == ["beat", "drain"]
