"""teatree.loops.master — DB ``Loop``-table-driven master fan-out (#1796).

The cutover gate: which loops fan out a tick is decided by the ``Loop`` rows
(enabled + ``is_due``), not code cadence. Integration-first against the real DB;
``iter_loops`` is patched to a small stub set so the assertions don't depend on
the seeded production loops.
"""

from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import Loop, Prompt
from teatree.loops.base import MiniLoop
from teatree.loops.master import build_loop_table_jobs


def _mini(name: str) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=lambda n=name, **_: [f"job-{n}"])


def _prompt(name: str = "demo-prompt") -> Prompt:
    """A reusable :class:`Prompt` FK target for loops under test (#2513)."""
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": "do x"})
    return prompt


@django.test.override_settings(USE_TZ=True)
class TestBuildLoopTableJobs(django.test.TestCase):
    def test_runs_only_enabled_and_due_loops(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="m-a", delay_seconds=60, prompt=_prompt())  # never run -> due
        Loop.objects.create(name="m-b", delay_seconds=60, prompt=_prompt(), last_run_at=now)  # cooling -> not due
        Loop.objects.create(name="m-c", delay_seconds=60, prompt=_prompt(), enabled=False)  # due but disabled
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("m-a"), _mini("m-b"), _mini("m-c"))):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-a" in jobs
        assert "job-m-b" not in jobs
        assert "job-m-c" not in jobs

    def test_marks_last_run_for_dispatched_loop(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="m-d", delay_seconds=60, prompt=_prompt())
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("m-d"),)):
            build_loop_table_jobs({}, now=now)
        assert Loop.objects.get(name="m-d").last_run_at == now

    def test_skips_registry_loop_with_no_row(self) -> None:
        now = timezone.now()
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("m-orphan"),)):
            jobs = build_loop_table_jobs({}, now=now)
        assert jobs == []

    def test_one_loop_raising_does_not_abort_the_rest(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="m-boom", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="m-ok", delay_seconds=60, prompt=_prompt())
        boom = MiniLoop(
            name="m-boom", default_cadence_seconds=60, build_jobs=lambda **_: (_ for _ in ()).throw(RuntimeError("x"))
        )
        with patch("teatree.loops.master.iter_loops", return_value=(boom, _mini("m-ok"))):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-ok" in jobs
        # the raising loop is NOT marked run; the healthy one is
        assert Loop.objects.get(name="m-boom").last_run_at is None
        assert Loop.objects.get(name="m-ok").last_run_at == now
