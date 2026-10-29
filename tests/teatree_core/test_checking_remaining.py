from collections import Counter
from datetime import UTC, datetime

from django.test import TestCase

from teatree.core.admission.lanes import ANSWERING_LANE_WIDTH
from teatree.core.checking_remaining import QueueState, remaining_steps
from teatree.core.modelkit.phases import MAINTENANCE_PHASES
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket


def test_reserved_pending_work_does_not_delay_coding_queue() -> None:
    pending = [
        Task(pk=index, phase=phase, status=Task.Status.PENDING)
        for index, phase in enumerate(
            ("architectural_review", "triage_assessing", "scanning_news", "answering", "coding"), start=1
        )
    ]
    queue = QueueState(
        now=datetime.now(UTC),
        pending=pending,
        claimed=Counter(),
        pending_counts=Counter(),
        occupied=Counter(),
        seats={"expensive": 1},
        oldest=None,
        oldest_phase=None,
    )

    assert queue.ahead(pending[-1], "coding") == 0
    assert queue.ahead(None, "coding") == 1


class TestQueueState(TestCase):
    def test_reports_reserved_lane_seats(self) -> None:
        now = datetime.now(UTC)
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        Task.objects.bulk_create(
            Task(ticket=ticket, session=session, phase=phase, admitted_at=now)
            for phase in ("answering", "architectural_review", "triage_assessing")
        )
        queue = QueueState.from_tasks([], [], now)

        assert queue.seats.keys() == {
            "expensive",
            "review",
            "retiring",
            "answering",
            "architectural_review",
            "maintenance",
        }
        assert queue.seats["answering"] == ANSWERING_LANE_WIDTH
        assert queue.seats["architectural_review"] == 1
        assert queue.seats["maintenance"] == len(MAINTENANCE_PHASES)
        assert queue.occupied == Counter({"answering": 1, "architectural_review": 1, "maintenance": 1})


def test_pre_pr_ladder_starts_after_current_state() -> None:
    assert remaining_steps(Ticket.State.CODED, "", has_pr=False, merge_safe=False)[:3] == (
        "testing",
        "factory review",
        "shipping",
    )


def test_pr_ladder_reopens_rework_and_respects_merge_safe() -> None:
    assert remaining_steps(Ticket.State.PR_OPENED, "coding", has_pr=True, merge_safe=True) == (
        "rework",
        "testing",
        "factory review",
        "independent review",
        "merge",
        "deploy",
    )
    assert remaining_steps(Ticket.State.REVIEW_REQUESTED, "", has_pr=True, merge_safe=True) == ("merge", "deploy")


def test_pr_with_a_testing_task_still_owes_factory_review() -> None:
    assert remaining_steps(Ticket.State.PR_OPENED, "testing", has_pr=True, merge_safe=False) == (
        "testing",
        "factory review",
        "independent review",
        "merge",
        "deploy",
    )


def test_review_variants_and_merged_work_keep_their_current_step() -> None:
    for phase in ("codex_reviewing", "codex_adversarial_reviewing", "e2e_reviewing", "critic_reviewing"):
        assert remaining_steps(Ticket.State.PR_OPENED, phase, has_pr=True, merge_safe=False)[:2] == (
            "factory review",
            "independent review",
        )
    assert remaining_steps(Ticket.State.MERGED, "coding", has_pr=False, merge_safe=False) == ("deploy",)
    assert remaining_steps(Ticket.State.RETRO_RECORDED, "coding", has_pr=False, merge_safe=False) == ("deploy",)
