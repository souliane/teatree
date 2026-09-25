"""Every enabled, timer-chained loop is carried by a live timer — or it has stopped.

The whole-system property behind the 61-minute ``issue_implementer`` outage
(souliane/teatree#4140). No unit test could see it: the dedup branch, the tiebreak
and the reconciler are each individually correct, and the failure is the relationship
between them — a corpse RUNNING row wins the id tiebreak, the losing fire returns
before the successor-first enqueue, and the chain is handed to something that will
never re-chain. Every cadence surface still read healthy throughout.

:func:`~teatree.loops.schedule_liveness.unscheduled_loops` is that reading, and the
anti-vacuity lane below drives the REAL :func:`~teatree.loops.timer_chains.loop_timer`
body against the real defect rather than hand-building the end state: a stranded
RUNNING row plus a higher-uuid live fire, which is the exact 05:25/05:55 sequence from
the incident. It is replayed BOTH ways round, because the fix is precisely that the
uuid coin-flip the incident report describes no longer decides whether the loop lives:
the corpse is disqualified, the surviving fire ticks, a successor is queued, and the
invariant names nothing — on either side of the flip. The assertions here were the
opposite before the fix, and the inversion is the evidence the defect closed rather
than moved.

``off_live_tick`` loops (``directive_loop``, ``dream``, ``outer_loop``) are excluded
deliberately: :mod:`teatree.loops.off_live_tick_driver` fires their own tick command,
so carrying no timer row is their correct steady state, and exposing them would be a
permanent false alarm.

A preset admitting zero loops is the invariant's PRECONDITION, not a breach of it:
step 0 of ``loop_timer`` halts without a successor precisely to terminate every chain
at its source, so a drained fleet under a stopping posture is the documented outcome of
an operator decision. The lane below pins that the alarm stays silent there, with the
same drained state under an admitting posture as its control.
"""

import datetime as dt
import types
import uuid

import django.test
import pytest
import typer
from django.tasks import TaskResultStatus
from django.utils import timezone
from django_tasks_db.models import DBTaskResult, normalize_uuid
from typer.testing import CliRunner

from teatree.cli.doctor.checks_loop import _check_loop_schedule_liveness
from teatree.core.models import Loop, Mode, ModeOverride
from teatree.loops import timer_chains
from teatree.loops.chain_membership import timer_chain_loop_names
from teatree.loops.registry import iter_loops
from teatree.loops.schedule_liveness import unscheduled_loops
from teatree.loops.timer_reconciler import ensure_loop_timers

#: The production DB backend, so an ``enqueue`` lands a real queryable timer row.
_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}

#: ``inbox`` is a real registered live-tick loop at a 60s cadence, so its tick
#: deadline is the 300s floor and a row started 10 minutes ago is a corpse.
_LOOP = "inbox"
_CORPSE_AGE = dt.timedelta(minutes=10)


def _fire(name: str, *, task_id: uuid.UUID) -> dict:
    """Invoke the real ``takes_context`` timer body with a duck-typed context."""
    ctx = types.SimpleNamespace(task_result=types.SimpleNamespace(id=task_id))
    return timer_chains.loop_timer.func(ctx, name)


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestScheduleLiveness(django.test.TestCase):
    def setUp(self) -> None:
        Loop.objects.all().delete()
        DBTaskResult.objects.all().delete()

    def _enable(self, name: str = _LOOP, **kwargs: object) -> Loop:
        defaults: dict[str, object] = {"delay_seconds": 60, "enabled": True, "last_run_at": None}
        defaults.update(kwargs)
        return Loop.objects.create(name=name, script=f"src/teatree/loops/{name}/loop.py", **defaults)

    def _zombie(self, name: str = _LOOP) -> uuid.UUID:
        """A RUNNING timer row left behind by a worker SIGKILLed mid-tick."""
        timer_chains.loop_timer.enqueue(name)
        row = timer_chains.pending_loop_timers(name)[0]
        DBTaskResult.objects.filter(id=row.id).update(
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now() - _CORPSE_AGE,
        )
        return uuid.UUID(normalize_uuid(row.id))

    def _names(self) -> list[str]:
        return [loop.name for loop in unscheduled_loops(timezone.now())]

    def _replay_the_incident(self, *, uuid_offset: int) -> None:
        """The 05:25/05:55 sequence: a corpse RUNNING row, then one live fire beside it."""
        self._enable()
        zombie_id = self._zombie()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                timer_chains,
                "run_deadlined_tick",
                lambda name, *, deadline: {"timed_out": False, "returncode": 0},
            )
            result = _fire(_LOOP, task_id=uuid.UUID(int=zombie_id.int + uuid_offset))

        assert result["action"] == "ticked"
        assert timer_chains.pending_loop_timers(_LOOP)
        assert self._names() == []

    def test_the_incident_sequence_keeps_the_chain(self) -> None:
        """The exact losing side of the flip. It used to forfeit the successor, not just its tick."""
        self._replay_the_incident(uuid_offset=+1)

    def test_the_other_side_of_the_uuid_coin_flip_keeps_the_chain_too(self) -> None:
        """The same sequence, flipped — the outcome must not depend on which uuid is smaller."""
        self._replay_the_incident(uuid_offset=-1)

    def test_a_reconciled_fleet_is_fully_scheduled(self) -> None:
        self._enable()
        self._enable("review", delay_seconds=300)
        ensure_loop_timers()
        assert self._names() == []

    def test_a_loop_with_no_timer_row_at_all_is_named(self) -> None:
        self._enable()
        assert self._names() == [_LOOP]

    def test_a_running_tick_inside_its_deadline_is_live(self) -> None:
        self._enable()
        timer_chains.loop_timer.enqueue(_LOOP)
        row = timer_chains.pending_loop_timers(_LOOP)[0]
        DBTaskResult.objects.filter(id=row.id).update(
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now(),
        )
        assert self._names() == []

    def test_a_recent_anchor_is_not_evidence_of_a_schedule(self) -> None:
        """The masking #4140 names: a manual tick bumps the anchor, not the chain."""
        row = self._enable()
        Loop.objects.mark_run(row.name, timezone.now())
        assert self._names() == [_LOOP]

    def test_a_disabled_loop_is_not_expected_to_carry_a_chain(self) -> None:
        self._enable(enabled=False)
        assert self._names() == []


def _doctor_verdict() -> tuple[bool, str]:
    """``_check_loop_schedule_liveness``'s verdict and the operator-facing text it echoed."""
    verdict: list[bool] = []
    app = typer.Typer()

    @app.command()
    def run() -> None:
        verdict.append(_check_loop_schedule_liveness())

    output = CliRunner().invoke(app, []).output
    return verdict[0], output


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestAStoppingPostureIsThePreconditionNotABreach(django.test.TestCase):
    """An operator who picks a posture admitting nothing has not broken the invariant.

    Step 0 of ``loop_timer`` halts WITHOUT a successor so the chain terminates at its
    source — so every loop drains into the exact state the alarm reads as a stopped
    chain. Red-lining a deliberate posture, with remediation advice that is wrong for the
    operator who picked it, is how a doctor teaches people to ignore it.
    """

    def setUp(self) -> None:
        Loop.objects.all().delete()
        DBTaskResult.objects.all().delete()
        Mode.objects.all().delete()
        ModeOverride.objects.all().delete()
        Loop.objects.create(name=_LOOP, script=f"src/teatree/loops/{_LOOP}/loop.py", delay_seconds=60)
        Mode.objects.create(name="stopped", entries={_LOOP: False})
        Mode.objects.create(name="running", entries={_LOOP: True})

    def _use(self, preset: str) -> None:
        ModeOverride.objects.set_override(preset, reason="pinned by the test")

    def _drain(self) -> None:
        """Fire the real timer body under the current posture and assert it halted the chain."""
        assert _fire(_LOOP, task_id=uuid.uuid4())["action"] == "halted"
        assert timer_chains.pending_loop_timers(_LOOP) == []

    def test_a_fleet_drained_by_a_stopping_posture_is_not_named(self) -> None:
        self._use("stopped")
        self._drain()
        assert unscheduled_loops(timezone.now()) == ()

    def test_the_doctor_does_not_fail_on_a_deliberate_stopping_posture(self) -> None:
        self._use("stopped")
        self._drain()
        passes, output = _doctor_verdict()
        assert passes is True
        assert "t3 worker ensure" not in output

    def test_the_same_drained_state_is_named_once_the_posture_admits_again(self) -> None:
        """The control: only the posture differs, and the alarm fires."""
        self._use("stopped")
        self._drain()
        self._use("running")
        assert [loop.name for loop in unscheduled_loops(timezone.now())] == [_LOOP]
        assert _doctor_verdict()[0] is False


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestOffLiveTickLoopsAreExcluded(django.test.TestCase):
    """The exclusion is deliberate and must stay derived from the registry, not a list."""

    def setUp(self) -> None:
        Loop.objects.all().delete()
        DBTaskResult.objects.all().delete()

    def test_registry_still_declares_off_live_tick_loops(self) -> None:
        assert {loop.name for loop in iter_loops() if loop.off_live_tick}

    def test_an_enabled_off_live_tick_loop_with_no_timer_is_not_named(self) -> None:
        off_tick = next(loop.name for loop in iter_loops() if loop.off_live_tick)
        Loop.objects.create(name=off_tick, script=f"src/teatree/loops/{off_tick}/loop.py", delay_seconds=60)
        assert off_tick not in timer_chain_loop_names()
        assert unscheduled_loops(timezone.now()) == ()
