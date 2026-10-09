"""``teatree.deploy.roll.Roller`` — the sequential N -> N+1 roll, driven against a fake compose engine."""

import os
import signal
from datetime import timedelta
from typing import cast
from unittest.mock import patch

import pytest
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Task, WorkerGeneration
from teatree.deploy.roll import (
    CLAIMING_SERVICES,
    RUNTIME_SERVICES,
    Roller,
    RollError,
    RollInterruptedError,
    RollOutcome,
    RollTiming,
    termination_signals_interrupt,
)
from teatree.generation import generation_image
from teatree.loop.drain import DrainPacing
from tests.factories import TaskFactory
from tests.teatree_deploy._fake_engine import FakeEngine, quiescing

N = "a" * 40
N1 = "b" * 40


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _engine(*, serving: str) -> FakeEngine:
    return FakeEngine(
        images={generation_image(N): N, generation_image(N1): N1},
        running=dict.fromkeys(RUNTIME_SERVICES, serving),
    )


def _roller(engine: FakeEngine, steps: list[str] | None = None, *, optional: frozenset[str] = frozenset()) -> Roller:
    clock = _Clock()
    timing = RollTiming(
        drain_timeout=60,
        verify_timeout=30,
        stable_seconds=10,
        pacing=DrainPacing(poll_interval=5, sleep=clock.sleep, monotonic=clock.monotonic),
    )
    return Roller(engine, timing=timing, on_step=(steps.append if steps is not None else None), optional=optional)


class TestAHappyRoll(TestCase):
    def setUp(self) -> None:
        WorkerGeneration.objects.boot(N)
        self.engine = _engine(serving=N)
        self.steps: list[str] = []

        self.report = _roller(self.engine, self.steps).roll(N1)

    def test_the_roll_reports_both_generations(self) -> None:
        assert self.report.outcome is RollOutcome.ROLLED
        assert (self.report.from_generation, self.report.to_generation) == (N, N1)

    def test_the_stack_is_replaced_in_order(self) -> None:
        assert self.engine.verbs() == ["image_revision", "stop", "run_init", "up", "promote"]
        assert self.engine.calls[1] == ("stop", *CLAIMING_SERVICES)
        assert self.engine.calls[3] == ("up", N1, *RUNTIME_SERVICES)

    def test_every_runtime_service_runs_the_new_generation(self) -> None:
        assert self.engine.running == dict.fromkeys(RUNTIME_SERVICES, N1)

    def test_the_new_generation_is_promoted(self) -> None:
        assert self.engine.promoted == N1

    def test_the_registry_retires_the_old_and_activates_the_new(self) -> None:
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.RETIRED
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE

    def test_a_generation_drain_never_touches_the_global_gate(self) -> None:
        assert self.engine.quiescing_at_stop == [False]

    def test_each_step_is_announced(self) -> None:
        assert self.steps == ["preflight", "register", "drain", "stop", "init", "up", "verify", "promote", "retire"]


class TestAFailedInitRollsBack(TestCase):
    def setUp(self) -> None:
        WorkerGeneration.objects.boot(N)
        self.engine = _engine(serving=N)
        self.engine.fail_init = True

        self.report = _roller(self.engine).roll(N1)

    def test_the_roll_reports_the_rollback_and_its_cause(self) -> None:
        assert self.report.outcome is RollOutcome.ROLLED_BACK
        assert "teatree-init exited 1" in self.report.detail

    def test_the_old_generation_serves_again(self) -> None:
        assert [call for call in self.engine.calls if call[0] == "up"][-1] == ("up", N, *RUNTIME_SERVICES)
        assert self.engine.running == dict.fromkeys(RUNTIME_SERVICES, N)
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE

    def test_the_new_generation_is_failed_with_the_cause(self) -> None:
        row = WorkerGeneration.objects.get(sha=N1)
        assert row.state == WorkerGeneration.State.FAILED
        assert "teatree-init exited 1" in row.failure_reason

    def test_nothing_is_promoted(self) -> None:
        assert "promote" not in self.engine.verbs()


class TestPromotionFailure(TestCase):
    def test_a_failed_tag_is_retried_after_verification_before_retiring_n(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.promoted = N
        engine.fail_promote_once = True

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED
        assert engine.calls.count(("promote", N1)) == 2
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.RETIRED
        assert engine.running == dict.fromkeys(RUNTIME_SERVICES, N1)
        assert generation_image(engine.promoted) == generation_image(N1)

    def _roll_with_a_tag_that_never_moves(self, *, init_applies: str = "") -> FakeEngine:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.promoted = N
        engine.fail_promote_of = N1
        engine.init_applies = init_applies

        with pytest.raises(RollError, match="verified and serving"):
            _roller(engine).roll(N1)

        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.DRAINING
        assert engine.running == dict.fromkeys(RUNTIME_SERVICES, N1)
        assert engine.promoted == N
        return engine

    def test_a_persistent_tag_failure_never_demotes_the_verified_generation(self) -> None:
        self._roll_with_a_tag_that_never_moves()

    def test_nor_after_its_init_migrated_the_schema(self) -> None:
        self._roll_with_a_tag_that_never_moves(init_applies="9999_zdd_promote_probe")

    def test_re_running_the_roll_promotes_and_retires_after_a_tag_failure(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.fail_promote_of = N1
        with pytest.raises(RollError):
            _roller(engine).roll(N1)
        engine.fail_promote_of = None

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ALREADY_CURRENT
        assert engine.promoted == N1
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.RETIRED


class TestAnUnverifiedGenerationRollsBack(TestCase):
    def test_an_admin_that_never_answers_restores_the_old_generation(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.admin_down_for = N1

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED_BACK
        assert "admin" in report.detail
        assert engine.running == dict.fromkeys(RUNTIME_SERVICES, N)
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.FAILED
        assert "promote" not in engine.verbs()

    def test_a_worker_that_never_activates_its_generation_is_not_verified(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.worker_boots = False

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED_BACK
        assert "has not activated" in report.detail
        assert "promote" not in engine.verbs()


class TestOnlyAServiceThatStaysUpVerifies(TestCase):
    def setUp(self) -> None:
        WorkerGeneration.objects.boot(N)
        self.engine = _engine(serving=N)
        self.engine.crash_looping = {"teatree-slack-listener"}
        self.engine.crash_loop_of = N1

    def test_a_crash_looping_service_that_reads_as_running_fails_the_roll(self) -> None:
        report = _roller(self.engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED_BACK
        assert "teatree-slack-listener has not stayed up for 10s" in report.detail
        assert "promote" not in self.engine.verbs()

    def test_a_service_declared_not_required_on_this_stack_does_not_gate_the_roll(self) -> None:
        report = _roller(self.engine, optional=frozenset({"teatree-slack-listener"})).roll(N1)

        assert report.outcome is RollOutcome.ROLLED

    def test_a_service_that_stays_up_verifies_only_after_the_window(self) -> None:
        self.engine.crash_looping = set()

        report = _roller(self.engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED
        assert self.engine.calls.count(("admin_answers",)) >= 3

    def test_the_worker_and_the_admin_can_never_be_optional(self) -> None:
        for essential in ("teatree-worker", "teatree-admin"):
            with pytest.raises(RollError, match="cannot be optional"):
                _roller(self.engine, optional=frozenset({essential}))

    def test_an_unknown_service_cannot_be_declared_optional(self) -> None:
        with pytest.raises(RollError, match="not a runtime service"):
            _roller(self.engine, optional=frozenset({"teatree-slack-lisener"}))


class TestARollbackIsVerifiedLikeARoll(TestCase):
    def test_a_restored_generation_that_does_not_verify_fails_loud(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.fail_init = True
        engine.admin_up = False

        with pytest.raises(RollError, match=rf"restored {N[:12]} but it did not verify.*admin does not answer"):
            _roller(engine).roll(N1)

    def test_a_restored_legacy_stack_must_be_running_again(self) -> None:
        engine = _engine(serving="")
        engine.fail_init = True
        engine.admin_up = False

        with pytest.raises(RollError, match="restored the legacy stack but it did not verify"):
            _roller(engine).roll(N1)

    def test_a_verified_restore_is_reported_as_rolled_back(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.fail_init = True

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED_BACK
        assert engine.calls[-1] == ("admin_answers",)


class TestARollNeverRollsBackAcrossTheSchema(TestCase):
    def setUp(self) -> None:
        WorkerGeneration.objects.boot(N)
        self.engine = _engine(serving=N)
        self.engine.init_applies = "9999_zdd_schema_probe"

    def test_a_failure_after_init_migrated_refuses_the_rollback(self) -> None:
        self.engine.fail_init = True

        with pytest.raises(RollError, match=r"applied core\.9999_zdd_schema_probe — refusing to roll back"):
            _roller(self.engine).roll(N1)

        assert ("up", N, *RUNTIME_SERVICES) not in self.engine.calls
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.FAILED
        assert "promote" not in self.engine.verbs()

    def test_an_unverified_generation_that_migrated_is_not_rolled_back_either(self) -> None:
        self.engine.admin_down_for = N1

        with pytest.raises(RollError, match="refusing to roll back"):
            _roller(self.engine).roll(N1)

        assert self.engine.running == dict.fromkeys(RUNTIME_SERVICES, N1)

    def test_a_migrating_roll_that_verifies_is_an_ordinary_roll(self) -> None:
        report = _roller(self.engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED


class TestAnInterruptedRollRestoresTheFence(TestCase):
    def test_an_interrupt_mid_roll_restores_the_serving_generation_then_propagates(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.interrupt_init = RollInterruptedError

        with pytest.raises(RollInterruptedError):
            _roller(engine).roll(N1)

        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.FAILED
        assert engine.running == dict.fromkeys(RUNTIME_SERVICES, N)

    def test_an_interrupted_first_cutover_reopens_the_global_gate(self) -> None:
        engine = _engine(serving="")
        engine.interrupt_init = KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            _roller(engine).roll(N1)

        assert not quiescing()

    def test_a_termination_signal_becomes_an_interrupt_the_roll_can_handle(self) -> None:
        with pytest.raises(RollInterruptedError, match="SIGTERM"), termination_signals_interrupt():
            os.kill(os.getpid(), signal.SIGTERM)

    def test_a_second_sigterm_during_the_restore_does_not_abandon_it(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.signal_init = signal.SIGTERM
        engine.signal_up_of = N

        with pytest.raises(RollInterruptedError), termination_signals_interrupt():
            _roller(engine).roll(N1)

        assert engine.running == dict.fromkeys(RUNTIME_SERVICES, N)
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE

    def test_the_previous_signal_handlers_come_back(self) -> None:
        before = signal.getsignal(signal.SIGTERM)

        with termination_signals_interrupt():
            pass

        assert signal.getsignal(signal.SIGTERM) is before


class TestAStrandedDrainIsResumedByTheNextRoll(TestCase):
    def test_a_generation_left_draining_with_no_successor_serves_again_and_is_rolled_from(self) -> None:
        WorkerGeneration.objects.boot(N).begin_drain(deadline=timezone.now() + timedelta(hours=1))
        engine = _engine(serving=N)

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED
        assert report.from_generation == N
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.RETIRED

    def test_rolling_to_the_stranded_generation_itself_just_reopens_it(self) -> None:
        WorkerGeneration.objects.boot(N).begin_drain(deadline=timezone.now() + timedelta(hours=1))
        engine = _engine(serving=N)

        report = _roller(engine).roll(N)

        assert report.outcome is RollOutcome.ALREADY_CURRENT
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE


class TestAFailedRollbackFailsLoud(TestCase):
    def test_both_causes_are_named(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.fail_init = True
        engine.fail_up_of = N

        with pytest.raises(RollError, match=r"teatree-init exited 1.*compose up of a+ failed"):
            _roller(engine).roll(N1)


class TestRollingIsIdempotent(TestCase):
    def test_rolling_to_the_serving_generation_changes_nothing(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        _roller(engine).roll(N1)
        engine.calls.clear()

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ALREADY_CURRENT
        assert "stop" not in engine.verbs()
        assert "run_init" not in engine.verbs()
        assert engine.promoted == N1
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE

    def test_a_roll_interrupted_before_retire_is_finished_on_rerun(self) -> None:
        WorkerGeneration.objects.boot(N).begin_drain(deadline=timezone.now())
        WorkerGeneration.objects.boot(N1)
        engine = _engine(serving=N1)

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ALREADY_CURRENT
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.RETIRED

    def test_a_missing_service_after_activation_is_started_on_retry(self) -> None:
        WorkerGeneration.objects.boot(N).begin_drain(deadline=timezone.now())
        WorkerGeneration.objects.boot(N1)
        engine = _engine(serving=N1)
        del engine.running["teatree-admin"]

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ALREADY_CURRENT
        assert ("up", N1, *RUNTIME_SERVICES) in engine.calls
        assert engine.running == dict.fromkeys(RUNTIME_SERVICES, N1)
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.RETIRED

    def test_a_failure_before_begin_drain_preserves_the_original_error(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)

        def fail_before_drain(step: str) -> None:
            if step == "drain":
                reason = "original pre-drain failure"
                raise ValueError(reason)

        with pytest.raises(ValueError, match="original pre-drain failure"):
            Roller(engine, on_step=fail_before_drain).roll(N1)

        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.FAILED
        assert "stop" not in engine.verbs()

    def test_a_failure_before_the_drain_leaves_a_legacy_stack_untouched(self) -> None:
        engine = _engine(serving="")

        def fail_before_drain(step: str) -> None:
            if step == "drain":
                reason = "original pre-drain failure"
                raise ValueError(reason)

        with pytest.raises(ValueError, match="original pre-drain failure"):
            Roller(engine, on_step=fail_before_drain).roll(N1)

        assert "stop" not in engine.verbs()
        assert "up" not in engine.verbs()
        assert not quiescing()

    def test_a_failed_generation_can_be_rolled_again(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.fail_init = True
        _roller(engine).roll(N1)
        engine.fail_init = False

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE


class TestTheFirstCutoverFromALegacyStack(TestCase):
    def test_a_legacy_stack_is_drained_globally_then_the_gate_reopens(self) -> None:
        engine = _engine(serving="")

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED
        assert report.from_generation == ""
        assert engine.quiescing_at_stop == [True]
        assert not quiescing()
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE

    def test_a_failed_first_cutover_restores_the_legacy_stack_and_its_admission(self) -> None:
        engine = _engine(serving="")
        engine.fail_init = True

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED_BACK
        assert [call for call in engine.calls if call[0] == "up"][-1] == ("up", "", *RUNTIME_SERVICES)
        assert not quiescing()


class TestPreflight(TestCase):
    def test_an_image_that_is_not_built_touches_nothing(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        del engine.images[generation_image(N1)]

        with pytest.raises(RollError, match=r"build-generation\.sh"):
            _roller(engine).roll(N1)

        assert engine.verbs() == ["image_revision"]
        assert not WorkerGeneration.objects.for_sha(N1).exists()

    def test_an_image_whose_label_disagrees_touches_nothing(self) -> None:
        engine = _engine(serving=N)
        engine.images[generation_image(N1)] = "c" * 40

        with pytest.raises(RollError, match="revision"):
            _roller(engine).roll(N1)

        assert engine.verbs() == ["image_revision"]

    def test_a_malformed_sha_is_refused(self) -> None:
        with pytest.raises(RollError, match="40-hex"):
            _roller(_engine(serving=N)).roll("main")

    def test_a_generation_behind_the_applied_schema_is_refused_before_anything_moves(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        MigrationRecorder(connection).record_applied("core", "9999_zdd_newer_than_this_code")

        with pytest.raises(RollError, match="behind the applied schema"):
            _roller(engine).roll(N1)

        assert engine.verbs() == ["image_revision"]
        assert not WorkerGeneration.objects.for_sha(N1).exists()


class TestTheRunningWorkerNamesWhatServes(TestCase):
    """A deploy outside the roller replaces the containers and leaves the registry naming the old generation."""

    def setUp(self) -> None:
        WorkerGeneration.objects.boot(N)
        self.engine = _engine(serving="")

    def test_a_legacy_worker_is_drained_globally_though_the_registry_names_a_generation(self) -> None:
        task = cast("Task", TaskFactory(status=Task.Status.PENDING))
        with patch.dict("os.environ", {"TEATREE_GENERATION": ""}):
            task.claim(claimed_by="loop", lease_seconds=3600)

        report = _roller(self.engine).roll(N1)

        assert report.from_generation == ""
        assert self.engine.quiescing_at_stop == [True]
        assert report.still_claimed == [task.pk]
        assert not quiescing()

    def test_the_generation_it_displaced_is_failed(self) -> None:
        _roller(self.engine).roll(N1)

        displaced = WorkerGeneration.objects.get(sha=N)
        assert displaced.state == WorkerGeneration.State.FAILED
        assert "the legacy stack" in displaced.failure_reason

    def test_a_failed_roll_restores_the_legacy_stack_that_was_serving(self) -> None:
        self.engine.fail_init = True

        report = _roller(self.engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED_BACK
        assert [call for call in self.engine.calls if call[0] == "up"][-1] == ("up", "", *RUNTIME_SERVICES)
        assert not quiescing()

    def test_a_stopped_legacy_worker_still_names_what_served(self) -> None:
        self.engine.stop(("teatree-worker",))
        self.engine.quiescing_at_stop.clear()

        report = _roller(self.engine).roll(N1)

        assert report.from_generation == ""
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.FAILED

    def test_a_running_generation_the_registry_does_not_list_as_serving_is_drained_globally(self) -> None:
        WorkerGeneration.objects.get(sha=N).fail(reason="failed by a starting lease that expired")
        engine = _engine(serving=N)

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED
        assert report.from_generation == N
        assert engine.quiescing_at_stop == [True]
        assert not quiescing()


class TestRollingBackToAPreviousGeneration(TestCase):
    def test_a_retired_generation_is_rolled_to_again(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        _roller(engine).roll(N1)

        report = _roller(engine).roll(N)

        assert report.outcome is RollOutcome.ROLLED
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.RETIRED

    def _left_draining_by_a_roll_interrupted_before_retire(self) -> FakeEngine:
        WorkerGeneration.objects.boot(N).begin_drain(deadline=timezone.now() + timedelta(hours=1))
        WorkerGeneration.objects.boot(N1)
        return _engine(serving=N1)

    def test_a_generation_left_draining_by_an_interrupted_roll_is_rolled_to_again(self) -> None:
        engine = self._left_draining_by_a_roll_interrupted_before_retire()

        report = _roller(engine).roll(N)

        assert report.outcome is RollOutcome.ROLLED
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.RETIRED

    def test_a_failed_roll_back_to_it_still_restores_what_serves(self) -> None:
        engine = self._left_draining_by_a_roll_interrupted_before_retire()
        engine.fail_init = True

        report = _roller(engine).roll(N)

        assert report.outcome is RollOutcome.ROLLED_BACK
        assert engine.running == dict.fromkeys(RUNTIME_SERVICES, N1)
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.FAILED


def _heal_the_drain_of_n_from_its_own_admin() -> None:
    long_ago = timezone.now() - timedelta(hours=2)
    WorkerGeneration.objects.filter(sha=N).update(drain_deadline=long_ago)
    WorkerGeneration.objects.filter(sha=N1).update(started_at=long_ago)
    assert WorkerGeneration.objects.reopen_stranded_drain(N)


class TestADrainReopenedMidRoll(TestCase):
    def test_a_generation_that_healed_its_own_drain_is_brought_back_up(self) -> None:
        WorkerGeneration.objects.boot(N)
        engine = _engine(serving=N)
        engine.on_init = _heal_the_drain_of_n_from_its_own_admin
        engine.fail_init = True

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED_BACK
        assert engine.running == dict.fromkeys(RUNTIME_SERVICES, N)
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE


class TestReRollingAGenerationThatFailedAfterItsInitMigrated(TestCase):
    """The roll refuses the rollback and leaves N+1 FAILED but running; re-rolling the same sha must restart it."""

    def setUp(self) -> None:
        WorkerGeneration.objects.boot(N)
        self.engine = _engine(serving=N)
        self.engine.init_applies = "0001_zdd_late_admin_probe"
        self.engine.admin_down_for = N1
        with pytest.raises(RollError, match="refusing to roll back"):
            _roller(self.engine).roll(N1)
        self.engine.init_applies = ""

    def test_the_same_sha_converges_once_it_verifies(self) -> None:
        self.engine.admin_down_for = None

        report = _roller(self.engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.RETIRED
        assert self.engine.promoted == N1
        assert not quiescing()

    def test_a_restart_that_still_does_not_verify_fails_it_and_reopens_admission(self) -> None:
        with pytest.raises(RollError, match="admin does not answer"):
            _roller(self.engine).roll(N1)

        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.FAILED
        assert not quiescing()


class TestADrainThatRunsOutOfGrace(TestCase):
    def test_the_roll_proceeds_and_names_the_stranded_claims(self) -> None:
        WorkerGeneration.objects.boot(N)
        task = cast("Task", TaskFactory(status=Task.Status.PENDING))
        with patch.dict("os.environ", {"TEATREE_GENERATION": N}):
            task.claim(claimed_by="loop", lease_seconds=3600)
        engine = _engine(serving=N)

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED
        assert report.still_claimed == [task.pk]
