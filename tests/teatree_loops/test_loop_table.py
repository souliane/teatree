"""teatree.loops.loop_table — DB ``Loop``-table-driven master fan-out (#1796).

The cutover gate: which loops fan out a tick is decided by the ``Loop`` rows
(enabled + ``is_due``), not code cadence. Integration-first against the real DB;
``iter_loops`` is patched to a small stub set so the assertions don't depend on
the seeded production loops.
"""

import datetime as dt
from typing import TYPE_CHECKING
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import Loop, LoopState, Prompt
from teatree.loops.base import MiniLoop
from teatree.loops.loop_table import build_loop_table_jobs

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob


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
        with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("m-a"), _mini("m-b"), _mini("m-c"))):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-a" in jobs
        assert "job-m-b" not in jobs
        assert "job-m-c" not in jobs

    def test_marks_last_run_for_dispatched_loop(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="m-d", delay_seconds=60, prompt=_prompt())
        with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("m-d"),)):
            build_loop_table_jobs({}, now=now)
        assert Loop.objects.get(name="m-d").last_run_at == now

    def test_skips_registry_loop_with_no_row(self) -> None:
        now = timezone.now()
        with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("m-orphan"),)):
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
        with patch("teatree.loops.loop_table.iter_loops", return_value=(off,)):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-dream" not in jobs
        assert jobs == []
        # never re-armed: its cadence anchor is untouched by the live tick
        assert Loop.objects.get(name="m-dream").last_run_at is None

    def test_only_filter_scopes_the_tick_to_one_named_loop(self) -> None:
        # #2650: the per-loop ``/loop`` fires ``t3 loops tick --loop <name>`` so
        # the master must be able to build jobs for EXACTLY one enabled, due row
        # (the rest stay untouched — their cadence anchors are not consumed).
        now = timezone.now()
        Loop.objects.create(name="m-only", delay_seconds=60, prompt=_prompt())  # enabled + due
        Loop.objects.create(name="m-other", delay_seconds=60, prompt=_prompt())  # enabled + due
        with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("m-only"), _mini("m-other"))):
            jobs = build_loop_table_jobs({}, now=now, only="m-only")
        assert "job-m-only" in jobs
        assert "job-m-other" not in jobs
        assert Loop.objects.get(name="m-only").last_run_at == now
        assert Loop.objects.get(name="m-other").last_run_at is None  # untouched

    def test_only_filter_still_honours_enabled_and_due(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="m-disabled", delay_seconds=60, prompt=_prompt(), enabled=False)
        with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("m-disabled"),)):
            jobs = build_loop_table_jobs({}, now=now, only="m-disabled")
        assert jobs == []

    def test_one_loop_raising_does_not_abort_the_rest(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="m-boom", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="m-ok", delay_seconds=60, prompt=_prompt())
        boom = MiniLoop(
            name="m-boom", default_cadence_seconds=60, build_jobs=lambda **_: (_ for _ in ()).throw(RuntimeError("x"))
        )
        with patch("teatree.loops.loop_table.iter_loops", return_value=(boom, _mini("m-ok"))):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-ok" in jobs
        # The anchor is now claimed atomically BEFORE build_jobs, so a loop that
        # wins the claim and then raises has already advanced its anchor — it is
        # simply not re-driven until its cadence elapses again (the price of
        # atomicity). The healthy sibling still runs.
        assert Loop.objects.get(name="m-boom").last_run_at == now
        assert Loop.objects.get(name="m-ok").last_run_at == now


@django.test.override_settings(USE_TZ=True)
class TestMasterHonoursLoopState(django.test.TestCase):
    """The master gate routes through the unified verdict (#2584).

    The #2513 cutover left ``build_loop_table_jobs`` gating only on
    ``Loop.enabled AND is_due`` — it ignored the durable ``LoopState`` control
    tier (``t3 loop pause`` / ``disable``, #1913). These pin that the master now
    reaches the SAME verdict ``LoopsConfig.is_enabled`` does: a ``Loop`` row that
    is ``enabled`` and ``is_due`` is STILL skipped (and not cadence-bumped) when
    a LoopState hold applies — and that the removed ``T3_LOOPS_DISABLED`` env var
    is now INERT (the master never reads it).
    """

    def test_loop_state_pause_skips_an_enabled_due_loop(self) -> None:
        # An enabled + never-run (due) Loop row, but held by a LoopState PAUSE:
        # the master must emit no job and must NOT consume the cadence anchor.
        now = timezone.now()
        Loop.objects.create(name="m-paused", delay_seconds=60, prompt=_prompt())
        LoopState.objects.pause("m-paused")
        with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("m-paused"),)):
            jobs = build_loop_table_jobs({}, now=now)
        assert jobs == []
        assert Loop.objects.get(name="m-paused").last_run_at is None

    def test_loop_state_disable_skips_an_enabled_due_loop(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="m-disabled-state", delay_seconds=60, prompt=_prompt())
        LoopState.objects.disable("m-disabled-state")
        with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("m-disabled-state"),)):
            jobs = build_loop_table_jobs({}, now=now)
        assert jobs == []
        assert Loop.objects.get(name="m-disabled-state").last_run_at is None

    def test_env_kill_switch_all_is_inert(self) -> None:
        # ``T3_LOOPS_DISABLED`` is removed — a set env var has NO effect: enabled
        # + due loops STILL fan out and ARE cadence-bumped. (DB-disable is the
        # control outcome — pinned by the LoopState tests above.)
        now = timezone.now()
        Loop.objects.create(name="m-env-a", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="m-env-b", delay_seconds=60, prompt=_prompt())
        registry = (_mini("m-env-a"), _mini("m-env-b"))
        with (
            patch.dict("os.environ", {"T3_LOOPS_DISABLED": "all"}),
            patch("teatree.loops.loop_table.iter_loops", return_value=registry),
        ):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-env-a" in jobs
        assert "job-m-env-b" in jobs
        assert Loop.objects.get(name="m-env-a").last_run_at == now
        assert Loop.objects.get(name="m-env-b").last_run_at == now

    def test_env_kill_switch_named_loop_is_inert(self) -> None:
        # A named ``T3_LOOPS_DISABLED=<name>`` is likewise inert — both loops run.
        now = timezone.now()
        Loop.objects.create(name="m-named-off", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="m-named-on", delay_seconds=60, prompt=_prompt())
        registry = (_mini("m-named-off"), _mini("m-named-on"))
        with (
            patch.dict("os.environ", {"T3_LOOPS_DISABLED": "m-named-off"}),
            patch("teatree.loops.loop_table.iter_loops", return_value=registry),
        ):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-named-off" in jobs
        assert "job-m-named-on" in jobs
        assert Loop.objects.get(name="m-named-off").last_run_at == now
        assert Loop.objects.get(name="m-named-on").last_run_at == now

    def test_held_loop_cadence_anchor_is_not_consumed(self) -> None:
        # A LoopState-held enabled + due loop's cadence anchor must be preserved
        # — the gate runs BEFORE mark_run, so last_run_at stays None.
        now = timezone.now()
        Loop.objects.create(name="m-anchor", delay_seconds=60, prompt=_prompt())
        LoopState.objects.pause("m-anchor")
        with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("m-anchor"),)):
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
        with patch("teatree.loops.loop_table.iter_loops", return_value=registry):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-target" in jobs
        assert "job-m-alias" not in jobs

    def test_script_row_pointing_at_its_own_module_dispatches_itself(self) -> None:
        # The seeded shape: a row's ``script`` is its OWN module, so it dispatches
        # its own jobs.
        now = timezone.now()
        Loop.objects.create(name="m-self", delay_seconds=60, script="src/teatree/loops/m-self/loop.py")
        with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("m-self"),)):
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
            patch("teatree.loops.loop_table.iter_loops", return_value=registry),
            self.assertLogs("teatree.loops.loop_table", level="ERROR") as logs,
        ):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-good" in jobs
        assert "job-m-stale" not in jobs
        assert any("m-stale" in line or "run.py" in line for line in logs.output)
        # The stale row wins its atomic cadence claim before the unresolvable
        # script raises, so its anchor advances (claimed before build) — it is not
        # re-driven until its cadence elapses again. The healthy sibling runs.
        assert Loop.objects.get(name="m-stale").last_run_at == now
        assert Loop.objects.get(name="m-good").last_run_at == now

    def test_prompt_row_dispatches_its_own_loop_jobs(self) -> None:
        # A prompt-backed row (arch_review shape) dispatches its OWN loop's jobs —
        # the column is read (prompt-backed) and the row's own loop fans out.
        now = timezone.now()
        Loop.objects.create(name="m-prompt", delay_seconds=60, prompt=_prompt())
        with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("m-prompt"),)):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-m-prompt" in jobs


@django.test.override_settings(USE_TZ=True)
class TestMarkRunIfUnchanged(django.test.TestCase):
    """The atomic cadence-anchor CAS (:meth:`LoopManager.mark_run_if_unchanged`)."""

    def test_wins_when_anchor_matches_the_read_value(self) -> None:
        before = timezone.now() - dt.timedelta(seconds=120)
        now = timezone.now()
        Loop.objects.create(name="cas-match", delay_seconds=60, prompt=_prompt(), last_run_at=before)
        won = Loop.objects.mark_run_if_unchanged("cas-match", previous_last_run_at=before, now=now)
        assert won is True
        assert Loop.objects.get(name="cas-match").last_run_at == now

    def test_loses_when_anchor_advanced_since_the_read(self) -> None:
        # A concurrent tick already moved the anchor: the CAS on the stale value
        # matches 0 rows and loses, leaving the anchor untouched.
        before = timezone.now() - dt.timedelta(seconds=120)
        advanced = timezone.now() - dt.timedelta(seconds=10)
        now = timezone.now()
        Loop.objects.create(name="cas-stale", delay_seconds=60, prompt=_prompt(), last_run_at=advanced)
        won = Loop.objects.mark_run_if_unchanged("cas-stale", previous_last_run_at=before, now=now)
        assert won is False
        assert Loop.objects.get(name="cas-stale").last_run_at == advanced

    def test_wins_on_first_run_from_null_anchor(self) -> None:
        # The never-run NULL anchor is matched by the same predicate (Django
        # renders ``last_run_at=None`` as ``IS NULL``).
        now = timezone.now()
        Loop.objects.create(name="cas-null", delay_seconds=60, prompt=_prompt())  # last_run_at is None
        won = Loop.objects.mark_run_if_unchanged("cas-null", previous_last_run_at=None, now=now)
        assert won is True
        assert Loop.objects.get(name="cas-null").last_run_at == now


@django.test.override_settings(USE_TZ=True)
class TestCadenceClaimIsAtomic(django.test.TestCase):
    """A master and a per-loop tick that read the same anchor drive the loop ONCE.

    The lost-update (TOCTOU) the cutover left open: a master
    ``build_loop_table_jobs(only=None)`` and a per-loop
    ``build_loop_table_jobs(only=<name>)`` that both read the same stale
    ``last_run_at`` would each build the loop's jobs and each bump the anchor —
    the loop is driven twice. The fix claims the anchor atomically (CAS) BEFORE
    building, so exactly one wins.

    The concurrent per-loop tick is interleaved from inside the master's
    ``build_jobs`` — a point both the pre-fix and post-fix code reach. On the
    PRE-FIX code (``mark_run`` runs AFTER ``build_jobs``) the master has not yet
    bumped the anchor, so the concurrent tick reads the SAME stale anchor and
    double-drives ⇒ ``produced == 2`` (RED). On the FIXED code the master claimed
    the anchor BEFORE ``build_jobs``, so the concurrent tick sees the advanced
    anchor, is not due, and skips ⇒ ``produced == 1`` (GREEN).
    """

    def test_concurrent_master_and_per_loop_drive_exactly_one(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="m-race", delay_seconds=60, prompt=_prompt())  # never-run → due
        concurrent: dict[str, list[_ScannerJob]] = {"jobs": []}
        per_loop_mini = MiniLoop(name="m-race", default_cadence_seconds=60, build_jobs=lambda **_: ["job-m-race"])
        fired = {"n": 0}

        def master_build_jobs(**_: object) -> list[str]:
            fired["n"] += 1
            if fired["n"] == 1:
                with patch("teatree.loops.loop_table.iter_loops", return_value=(per_loop_mini,)):
                    concurrent["jobs"] = build_loop_table_jobs({}, now=now, only="m-race")
            return ["job-m-race"]

        master_mini = MiniLoop(name="m-race", default_cadence_seconds=60, build_jobs=master_build_jobs)
        with patch("teatree.loops.loop_table.iter_loops", return_value=(master_mini,)):
            master_jobs = build_loop_table_jobs({}, now=now, only=None)

        produced = ("job-m-race" in master_jobs) + ("job-m-race" in concurrent["jobs"])
        assert produced == 1, f"loop driven {produced}x (master={master_jobs}, per_loop={concurrent['jobs']})"
        assert Loop.objects.get(name="m-race").last_run_at == now
