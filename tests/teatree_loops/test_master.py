"""teatree.loops.master — DB ``Loop``-table-driven master fan-out (#1796).

The cutover gate: which loops fan out a tick is decided by the ``Loop`` rows
(enabled + ``is_due``), not code cadence. Integration-first against the real DB;
``iter_loops`` is patched to a small stub set so the assertions don't depend on
the seeded production loops.
"""

from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import Loop, LoopState, Prompt
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

    def test_off_live_tick_loop_is_never_picked_up(self) -> None:
        # An off_live_tick loop (the heavy ``dream`` pass, #1933 § 3) is enabled
        # and due, yet the live master tick must NEVER invoke its build_jobs or
        # bump its last_run_at — it is driven by its own low-frequency cron.
        now = timezone.now()
        Loop.objects.create(name="m-dream", delay_seconds=60, prompt=_prompt())  # enabled + never run -> due
        off = MiniLoop(
            name="m-dream",
            default_cadence_seconds=60,
            build_jobs=lambda **_: ["job-m-dream"],
            off_live_tick=True,
        )
        with patch("teatree.loops.master.iter_loops", return_value=(off,)):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-dream" not in jobs
        assert jobs == []
        # never re-armed: its cadence anchor is untouched by the live tick
        assert Loop.objects.get(name="m-dream").last_run_at is None

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


@django.test.override_settings(USE_TZ=True)
class TestMasterHonoursLoopStateAndEnv(django.test.TestCase):
    """The master gate routes through the unified verdict (#2584).

    The #2513 cutover left ``build_loop_table_jobs`` gating only on
    ``Loop.enabled AND is_due`` — it ignored both the durable ``LoopState``
    control tier (``t3 loop pause`` / ``disable``, #1913) and the
    ``T3_LOOPS_DISABLED`` env kill-switch. These pin that the master now reaches
    the SAME verdict ``LoopsConfig.is_enabled`` does: a ``Loop`` row that is
    ``enabled`` and ``is_due`` is STILL skipped (and not cadence-bumped) when a
    LoopState hold or the env kill-switch applies.
    """

    def test_loop_state_pause_skips_an_enabled_due_loop(self) -> None:
        # An enabled + never-run (due) Loop row, but held by a LoopState PAUSE:
        # the master must emit no job and must NOT consume the cadence anchor.
        now = timezone.now()
        Loop.objects.create(name="m-paused", delay_seconds=60, prompt=_prompt())
        LoopState.objects.pause("m-paused")
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("m-paused"),)):
            jobs = build_loop_table_jobs({}, now=now)
        assert jobs == []
        assert Loop.objects.get(name="m-paused").last_run_at is None

    def test_loop_state_disable_skips_an_enabled_due_loop(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="m-disabled-state", delay_seconds=60, prompt=_prompt())
        LoopState.objects.disable("m-disabled-state")
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("m-disabled-state"),)):
            jobs = build_loop_table_jobs({}, now=now)
        assert jobs == []
        assert Loop.objects.get(name="m-disabled-state").last_run_at is None

    def test_env_kill_switch_all_skips_every_enabled_due_loop(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="m-env-a", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="m-env-b", delay_seconds=60, prompt=_prompt())
        registry = (_mini("m-env-a"), _mini("m-env-b"))
        with (
            patch.dict("os.environ", {"T3_LOOPS_DISABLED": "all"}),
            patch("teatree.loops.master.iter_loops", return_value=registry),
        ):
            jobs = build_loop_table_jobs({}, now=now)
        assert jobs == []
        assert Loop.objects.get(name="m-env-a").last_run_at is None
        assert Loop.objects.get(name="m-env-b").last_run_at is None

    def test_env_kill_switch_named_loop(self) -> None:
        # T3_LOOPS_DISABLED=<name> kills only the named loop; the sibling runs.
        now = timezone.now()
        Loop.objects.create(name="m-named-off", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="m-named-on", delay_seconds=60, prompt=_prompt())
        registry = (_mini("m-named-off"), _mini("m-named-on"))
        with (
            patch.dict("os.environ", {"T3_LOOPS_DISABLED": "m-named-off"}),
            patch("teatree.loops.master.iter_loops", return_value=registry),
        ):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-named-off" not in jobs
        assert "job-m-named-on" in jobs
        assert Loop.objects.get(name="m-named-off").last_run_at is None
        assert Loop.objects.get(name="m-named-on").last_run_at == now

    def test_env_kill_switch_all_still_runs_always_on_loop(self) -> None:
        # The env kill-switch respects an always_on loop only via its flag —
        # an always_on loop still fans out even under T3_LOOPS_DISABLED=all.
        now = timezone.now()
        Loop.objects.create(name="m-always", delay_seconds=60, prompt=_prompt())
        always = MiniLoop(
            name="m-always",
            default_cadence_seconds=60,
            build_jobs=lambda **_: ["job-m-always"],
            always_on=True,
        )
        with (
            patch.dict("os.environ", {"T3_LOOPS_DISABLED": "all"}),
            patch("teatree.loops.master.iter_loops", return_value=(always,)),
        ):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-always" in jobs
        assert Loop.objects.get(name="m-always").last_run_at == now

    def test_held_loop_cadence_anchor_is_not_consumed(self) -> None:
        # A LoopState-held enabled + due loop's cadence anchor must be preserved
        # — the gate runs BEFORE mark_run, so last_run_at stays None.
        now = timezone.now()
        Loop.objects.create(name="m-anchor", delay_seconds=60, prompt=_prompt())
        LoopState.objects.pause("m-anchor")
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("m-anchor"),)):
            build_loop_table_jobs({}, now=now)
        assert Loop.objects.get(name="m-anchor").last_run_at is None


@django.test.override_settings(USE_TZ=True)
class TestMasterColumnIsLoadBearing(django.test.TestCase):
    """The live tick dispatches each row via ITS OWN ``script``/``prompt`` column.

    The #2513 regression: the master selected behaviour by a name-only registry
    lookup, so the DB ``script`` column was dead. These pin that the column is
    now LOAD-BEARING — which loop's jobs fan out is decided by reading
    ``Loop.script`` (resolved to a loop name), not by the row's name.
    """

    def test_dispatch_follows_the_script_column_not_the_row_name(self) -> None:
        # A row NAMED "m-alias" whose ``script`` points at "m-target"'s OWN module
        # must dispatch "m-target"'s jobs — proving the column drives selection.
        # On the pre-fix name-only lookup this would dispatch "m-alias".
        now = timezone.now()
        Loop.objects.create(name="m-alias", delay_seconds=60, script="src/teatree/loops/m-target/loop.py")
        registry = (_mini("m-alias"), _mini("m-target"))
        with patch("teatree.loops.master.iter_loops", return_value=registry):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-target" in jobs
        assert "job-m-alias" not in jobs

    def test_script_row_pointing_at_its_own_module_dispatches_itself(self) -> None:
        # The seeded shape: a row's ``script`` is its OWN module, so it dispatches
        # its own jobs.
        now = timezone.now()
        Loop.objects.create(name="m-self", delay_seconds=60, script="src/teatree/loops/m-self/loop.py")
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("m-self"),)):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-self" in jobs

    def test_stale_shared_script_row_is_logged_and_skipped_never_silent(self) -> None:
        # A row still holding the retired shared ``run.py`` does NOT resolve to a
        # registered loop module: the resolver raises loudly, the master logs it
        # and skips that row (never a silent no-op), and the healthy sibling still
        # runs — one bad row never aborts the tick. The stale row is not bumped.
        now = timezone.now()
        Loop.objects.create(name="m-stale", delay_seconds=60, script="src/teatree/loops/run.py")
        Loop.objects.create(name="m-good", delay_seconds=60, script="src/teatree/loops/m-good/loop.py")
        registry = (_mini("m-stale"), _mini("m-good"))
        with (
            patch("teatree.loops.master.iter_loops", return_value=registry),
            self.assertLogs("teatree.loops.master", level="ERROR") as logs,
        ):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-good" in jobs
        assert "job-m-stale" not in jobs
        assert any("m-stale" in line or "run.py" in line for line in logs.output)
        assert Loop.objects.get(name="m-stale").last_run_at is None
        assert Loop.objects.get(name="m-good").last_run_at == now

    def test_prompt_row_dispatches_its_own_loop_jobs(self) -> None:
        # A prompt-backed row (arch_review shape) dispatches its OWN loop's jobs —
        # the column is read (prompt-backed) and the row's own loop fans out.
        now = timezone.now()
        Loop.objects.create(name="m-prompt", delay_seconds=60, prompt=_prompt())
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("m-prompt"),)):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-prompt" in jobs
