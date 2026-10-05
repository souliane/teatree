"""``teatree.core.admission.generation_admission`` — the claim verdict and the stranded-drain heal."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from functools import partial
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.managers_task_claim import claim_admission_block_reason, claim_when_admitted
from teatree.core.models import Session, Task, Ticket, WorkerGeneration

_N = "a" * 40
_N1 = "b" * 40


@contextmanager
def _running_as(sha: str) -> Iterator[None]:
    with patch.dict("os.environ", {"TEATREE_GENERATION": sha}):
        yield


class TestADrainStrandedPastItsDeadlineReopens(TestCase):
    def setUp(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        self.task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, overlay="test"), phase="coding"
        )
        WorkerGeneration.objects.boot(_N).begin_drain(deadline=timezone.now() - timedelta(seconds=1))

    def test_a_worker_whose_drain_expired_with_no_successor_claims_again(self) -> None:
        with _running_as(_N):
            assert Task.objects.claim_next_pending(claimed_by="worker-1") == self.task

        assert WorkerGeneration.objects.state_of(_N) == WorkerGeneration.State.ACTIVE

    def test_a_single_task_claim_heals_through_the_claim_window(self) -> None:
        with _running_as(_N):
            refusal = claim_when_admitted(partial(self.task.claim, claimed_by="worker-1"))

        assert refusal == ""
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.CLAIMED
        assert WorkerGeneration.objects.state_of(_N) == WorkerGeneration.State.ACTIVE

    def test_a_heal_is_seen_by_the_very_next_admission_check(self) -> None:
        with _running_as(_N):
            assert claim_admission_block_reason() == "this worker's generation aaaaaaaaaaaa is draining"

            assert Task.objects.claim_next_pending(claimed_by="worker-1") == self.task

    def test_the_admission_read_reports_and_writes_nothing(self) -> None:
        with _running_as(_N):
            assert claim_admission_block_reason() == "this worker's generation aaaaaaaaaaaa is draining"

        assert WorkerGeneration.objects.state_of(_N) == WorkerGeneration.State.DRAINING

    def test_a_successor_still_starting_keeps_it_closed(self) -> None:
        WorkerGeneration.objects.register(_N1)

        with _running_as(_N):
            assert claim_admission_block_reason() == "this worker's generation aaaaaaaaaaaa is draining"
