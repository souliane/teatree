"""``WorkerGeneration`` — the registry of which immutable code generation is live."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_fsm import TransitionNotAllowed

from teatree.core.managers_task_claim import claim_when_admitted, quiesce_fence_generation
from teatree.core.models import WorkerGeneration
from teatree.core.models.worker_generation import WorkerGenerationQuerySet

N = "a" * 40
N1 = "b" * 40


def _active(sha: str = N) -> WorkerGeneration:
    return WorkerGeneration.objects.boot(sha)


class TestRegister(TestCase):
    def test_a_new_generation_starts_with_its_derived_image(self) -> None:
        row = WorkerGeneration.objects.register(N)

        assert row.state == WorkerGeneration.State.STARTING
        assert row.image == f"teatree-factory:{N}"

    def test_registering_twice_is_the_same_row(self) -> None:
        first = WorkerGeneration.objects.register(N)
        first.activate()

        again = WorkerGeneration.objects.register(N)

        assert again.pk == first.pk
        assert again.state == WorkerGeneration.State.ACTIVE
        assert WorkerGeneration.objects.count() == 1

    def test_registering_a_failed_generation_retries_it(self) -> None:
        WorkerGeneration.objects.register(N).fail(reason="init exited 1")

        row = WorkerGeneration.objects.register(N)

        assert row.state == WorkerGeneration.State.STARTING
        assert row.failure_reason == ""


class TestBoot(TestCase):
    def test_boot_registers_and_activates(self) -> None:
        row = _active()

        assert row.state == WorkerGeneration.State.ACTIVE
        assert row.activated_at is not None

    def test_boot_never_revives_a_draining_generation(self) -> None:
        _active().begin_drain(deadline=timezone.now() + timedelta(minutes=30))

        assert WorkerGeneration.objects.boot(N).state == WorkerGeneration.State.DRAINING

    def test_boot_never_revives_a_retired_generation(self) -> None:
        row = _active()
        row.begin_drain(deadline=timezone.now())
        row.retire()

        assert WorkerGeneration.objects.boot(N).state == WorkerGeneration.State.RETIRED


class TestDrain(TestCase):
    def test_a_drain_that_lost_the_race_joins_the_one_that_won(self) -> None:
        stale = _active(N)
        winner = WorkerGeneration.objects.get(sha=N)
        winner.begin_drain(deadline=timezone.now() + timedelta(minutes=5))

        stale.begin_drain(deadline=timezone.now() + timedelta(hours=5))

        stale.refresh_from_db()
        assert stale.state == WorkerGeneration.State.DRAINING
        assert stale.drain_deadline is not None
        assert stale.drain_deadline < timezone.now() + timedelta(hours=1)

    def test_begin_drain_records_the_deadline(self) -> None:
        deadline = timezone.now() + timedelta(minutes=30)
        row = _active()

        row.begin_drain(deadline=deadline)

        row.refresh_from_db()
        assert row.state == WorkerGeneration.State.DRAINING
        assert row.drain_deadline == deadline
        assert row.drain_requested_at is not None

    def test_begin_drain_advances_the_quiesce_fence(self) -> None:
        row = _active()
        before = quiesce_fence_generation()

        row.begin_drain(deadline=timezone.now())

        assert quiesce_fence_generation() == before + 1

    def test_a_drain_whose_fence_fails_leaves_the_generation_active(self) -> None:
        row = _active()

        with (
            patch("teatree.core.models.worker_generation.advance_quiesce_fence", side_effect=RuntimeError("fence")),
            pytest.raises(RuntimeError, match="fence"),
        ):
            row.begin_drain(deadline=timezone.now())

        row.refresh_from_db()
        assert row.state == WorkerGeneration.State.ACTIVE
        assert row.drain_deadline is None

    def test_a_drain_resumes_back_to_active(self) -> None:
        row = _active()
        row.begin_drain(deadline=timezone.now())

        row.resume()

        row.refresh_from_db()
        assert row.state == WorkerGeneration.State.ACTIVE
        assert row.drain_deadline is None

    def test_a_drained_generation_retires(self) -> None:
        row = _active()
        row.begin_drain(deadline=timezone.now())

        row.retire()

        row.refresh_from_db()
        assert row.state == WorkerGeneration.State.RETIRED
        assert row.retired_at is not None

    def test_only_an_active_generation_can_drain(self) -> None:
        with pytest.raises(TransitionNotAllowed):
            WorkerGeneration.objects.register(N).begin_drain(deadline=timezone.now())

    def test_an_active_generation_cannot_retire_without_draining(self) -> None:
        with pytest.raises(TransitionNotAllowed):
            _active().retire()


class TestFail(TestCase):
    def test_a_starting_generation_fails_with_its_reason(self) -> None:
        row = WorkerGeneration.objects.register(N)

        row.fail(reason="init exited 1")

        row.refresh_from_db()
        assert row.state == WorkerGeneration.State.FAILED
        assert row.failure_reason == "init exited 1"

    def test_an_activated_generation_that_failed_verification_fails(self) -> None:
        row = _active()

        row.fail(reason="admin never answered")

        assert row.state == WorkerGeneration.State.FAILED

    def test_a_retired_generation_cannot_fail(self) -> None:
        row = _active()
        row.begin_drain(deadline=timezone.now())
        row.retire()

        with pytest.raises(TransitionNotAllowed):
            row.fail(reason="late")


class TestQueries(TestCase):
    def test_the_serving_generation_is_the_most_recently_activated(self) -> None:
        _active(N)
        _active(N1)

        assert WorkerGeneration.objects.serving().sha == N1

    def test_no_active_generation_means_the_legacy_stack_serves(self) -> None:
        WorkerGeneration.objects.register(N)

        assert WorkerGeneration.objects.serving() is None

    def test_live_excludes_retired_and_failed(self) -> None:
        retired = _active(N)
        retired.begin_drain(deadline=timezone.now())
        retired.retire()
        WorkerGeneration.objects.register("c" * 40).fail(reason="x")
        _active(N1)

        assert [row.sha for row in WorkerGeneration.objects.live()] == [N1]

    def test_state_of_an_unknown_generation_is_empty(self) -> None:
        assert WorkerGeneration.objects.state_of(N) == ""
        _active(N)
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE


def _draining(sha: str = N, *, deadline_in: timedelta) -> WorkerGeneration:
    row = _active(sha)
    row.begin_drain(deadline=timezone.now() + deadline_in)
    return row


class TestAStrandedDrainReopens(TestCase):
    def test_an_expired_drain_with_no_successor_reopens(self) -> None:
        _draining(deadline_in=-timedelta(seconds=1))

        assert WorkerGeneration.objects.reopen_stranded_drain(N) is True
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE

    def test_a_drain_still_inside_its_deadline_stays_closed(self) -> None:
        _draining(deadline_in=timedelta(minutes=5))

        assert WorkerGeneration.objects.reopen_stranded_drain(N) is False
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.DRAINING

    def test_an_expired_drain_whose_successor_serves_stays_closed(self) -> None:
        _draining(deadline_in=-timedelta(seconds=1))
        _active(N1)

        assert WorkerGeneration.objects.reopen_stranded_drain(N) is False

    def test_a_starting_successor_inside_its_lease_keeps_the_drain_closed(self) -> None:
        _draining(deadline_in=-timedelta(seconds=1))
        successor = WorkerGeneration.objects.register(N1)

        assert WorkerGeneration.objects.reopen_stranded_drain(N) is False
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.DRAINING
        assert WorkerGeneration.objects.state_of(successor.sha) == WorkerGeneration.State.STARTING

    def test_a_lost_roller_expires_its_successor_and_the_claim_path_reopens(self) -> None:
        successor = WorkerGeneration.objects.register(N1)
        row = _draining(deadline_in=timedelta(minutes=30))
        after_lease = row.drain_deadline + timedelta(minutes=11)

        with (
            patch.dict("os.environ", {"TEATREE_GENERATION": N}),
            patch("teatree.core.models.worker_generation.timezone.now", return_value=after_lease),
        ):
            assert claim_when_admitted(lambda: None) == ""

        row.refresh_from_db()
        successor.refresh_from_db()
        assert row.state == WorkerGeneration.State.ACTIVE
        assert successor.state == WorkerGeneration.State.FAILED
        assert successor.failure_reason
        assert WorkerGeneration.objects.boot(N1).state == WorkerGeneration.State.FAILED
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE

    def test_a_generation_that_is_not_draining_is_left_alone(self) -> None:
        _active(N)

        assert WorkerGeneration.objects.reopen_stranded_drain(N) is False
        assert WorkerGeneration.objects.reopen_stranded_drain(N1) is False

    def test_a_generation_with_nothing_to_heal_opens_no_write_transaction(self) -> None:
        _draining(deadline_in=timedelta(minutes=5))

        with CaptureQueriesContext(connection) as queries:
            assert WorkerGeneration.objects.reopen_stranded_drain(N) is False

        assert [query["sql"] for query in queries.captured_queries if "SAVEPOINT" in query["sql"]] == []


class TestAStrandedDrainStillServes(TestCase):
    def test_a_draining_generation_with_nothing_active_is_the_one_serving(self) -> None:
        _draining(deadline_in=timedelta(hours=1))

        assert WorkerGeneration.objects.serving().sha == N

    def test_an_active_generation_wins_over_a_leftover_drain(self) -> None:
        _draining(deadline_in=timedelta(hours=1))
        _active(N1)

        assert WorkerGeneration.objects.serving().sha == N1


class TestTransitionsLockTheRow(TestCase):
    def test_every_transition_rereads_the_state_under_a_row_lock(self) -> None:
        row = WorkerGeneration.objects.register(N)
        locked: list[bool] = []
        real = WorkerGenerationQuerySet.select_for_update

        def _spy(queryset: WorkerGenerationQuerySet, **kwargs: bool) -> WorkerGenerationQuerySet:
            locked.append(True)
            return real(queryset, **kwargs)

        with patch.object(WorkerGenerationQuerySet, "select_for_update", _spy):
            row.activate()

        assert locked == [True]
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE

    def test_a_stale_instance_moves_from_the_rows_current_state(self) -> None:
        stale = WorkerGeneration.objects.register(N)
        WorkerGeneration.objects.get(sha=N).activate()

        with pytest.raises(TransitionNotAllowed):
            stale.activate()
