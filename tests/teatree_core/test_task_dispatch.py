"""One ranked admission walk serves both the post_save receiver and the queue drain (#5051).

Admission used to book the row being saved whenever its lane had room, so a freshly
created task overtook every older row already waiting in the same lane, and a freed seat
sat empty until the next five-minute drain. These drive the real receiver and the real
drain; only the governor's box and quota probes are pinned.
"""

import datetime as dt
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone

from teatree.core import agent_admission as gate_mod
from teatree.core import task_dispatch as task_dispatch_mod
from teatree.core.admission.lanes import ANSWERING_LANE_WIDTH
from teatree.core.admission_governor import MachineSignal, QuotaSignal
from teatree.core.models import ConfigSetting, ModeOverride, PullRequest, Session, Task, Ticket
from teatree.core.signals import _auto_enqueue_task
from teatree.core.task_dispatch import admit_waiting_tasks
from teatree.core.tasks import drain_queue_body
from tests.teatree_core.conftest import HEALTHY_MACHINE_SIGNAL, HEALTHY_QUOTA_SIGNAL

#: The governor ceiling the pinned 8-core healthy box derives.
_CODING_CEILING = 4
#: The shipped ``cheap_phase_admission_ceiling``.
_REVIEW_WIDTH = 2


class _AdmissionCase(TestCase):
    def setUp(self) -> None:
        for name, signal in (
            ("read_quota_signal", HEALTHY_QUOTA_SIGNAL),
            ("read_machine_signal", HEALTHY_MACHINE_SIGNAL),
        ):
            probe = patch.object(gate_mod, name, return_value=signal)
            probe.start()
            self.addCleanup(probe.stop)
        runner = patch.object(task_dispatch_mod, "execute_task")
        runner.start()
        self.addCleanup(runner.stop)
        self.ticket = Ticket.objects.create()

    def _task(self, phase: str, *, status: str = Task.Status.PENDING, ticket: Ticket | None = None) -> Task:
        owner = ticket or self.ticket
        return Task.objects.create(
            ticket=owner,
            session=Session.objects.create(ticket=owner),
            status=status,
            phase=phase,
            lease_expires_at=timezone.now() + dt.timedelta(hours=1) if status == Task.Status.CLAIMED else None,
        )

    def _fill_the_review_lane(self) -> list[Task]:
        return [self._task("reviewing", status=Task.Status.CLAIMED) for _ in range(_REVIEW_WIDTH)]

    @staticmethod
    def _admitted(task: Task) -> bool:
        task.refresh_from_db()
        return task.admitted_at is not None


class TestACreatedRowDoesNotOvertakeAnOlderWaiter(_AdmissionCase):
    def test_the_older_waiter_takes_the_freed_seat_not_the_new_row(self) -> None:
        running = self._fill_the_review_lane()
        older = self._task("reviewing")
        Task.objects.filter(pk=running[0].pk).update(status=Task.Status.COMPLETED)

        newer = self._task("critic_reviewing")

        assert self._admitted(older)
        assert not self._admitted(newer)


class TestAWalkSkipsRowsAlreadyDispatched(_AdmissionCase):
    def test_the_next_walk_neither_rebooks_nor_reports_a_seated_row(self) -> None:
        first = self._task("reviewing")
        assert self._admitted(first)

        with self.assertNoLogs("teatree.core.agent_admission", level="WARNING"):
            self._task("reviewing")


class TestAFreedSeatIsRefilledInTheSameSave(_AdmissionCase):
    def test_a_task_reaching_a_terminal_state_admits_the_oldest_waiter(self) -> None:
        running = self._fill_the_review_lane()
        waiting = self._task("reviewing")
        assert not self._admitted(waiting)

        running[0].fail(reason="agent exited", by_holder=True)

        assert self._admitted(waiting)


class TestTheDrainAdmitsExpeditedWorkFirst(_AdmissionCase):
    def setUp(self) -> None:
        super().setUp()
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")

    def _one_free_seat_two_waiters(self, *, expedited: bool) -> tuple[Task, Task]:
        for _ in range(_CODING_CEILING - 1):
            self._task("coding", status=Task.Status.CLAIMED)
        older = self._task("coding")
        newer = self._task("planning", ticket=Ticket.objects.create(expedited=expedited))
        return older, newer

    def test_a_newer_expedited_row_takes_the_last_seat(self) -> None:
        older, newer = self._one_free_seat_two_waiters(expedited=True)

        assert drain_queue_body()["enqueued"] == [newer.pk]
        assert not self._admitted(older)

    def test_control_without_the_flag_the_older_row_goes_first(self) -> None:
        older, newer = self._one_free_seat_two_waiters(expedited=False)

        assert drain_queue_body()["enqueued"] == [older.pk]
        assert not self._admitted(newer)

    def test_full_reviews_leave_the_expedited_push_reachable_behind_an_older_review(self) -> None:
        self._fill_the_review_lane()
        older_review = self._task("reviewing")
        shipping = self._task("shipping", ticket=Ticket.objects.create(expedited=True))

        assert drain_queue_body()["enqueued"] == [shipping.pk]
        assert not self._admitted(older_review)
        assert self._admitted(shipping)

    def test_a_shipping_and_reviewing_claim_leave_room_for_another_review(self) -> None:
        self._task("shipping", status=Task.Status.CLAIMED)
        self._task("reviewing", status=Task.Status.CLAIMED)
        waiting = self._task("reviewing")

        assert drain_queue_body()["enqueued"] == [waiting.pk]
        assert self._admitted(waiting)

    def test_retiring_width_tracks_the_setting(self) -> None:
        ConfigSetting.objects.set_value("cheap_phase_admission_ceiling", 3)
        shipping = [self._task("shipping") for _ in range(4)]

        assert drain_queue_body()["enqueued"] == [task.pk for task in shipping[:3]]
        assert all(self._admitted(task) for task in shipping[:3])
        assert not self._admitted(shipping[3])


class TestTheDrainServesStartedPullRequests(_AdmissionCase):
    def setUp(self) -> None:
        super().setUp()
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")

    def _fill_coding_lane(self) -> None:
        for _ in range(_CODING_CEILING - 1):
            self._task("coding", status=Task.Status.CLAIMED)

    def test_started_pull_request_precedes_older_coding_task(self) -> None:
        self._fill_coding_lane()
        older = self._task("coding")
        ticket = Ticket.objects.create()
        started = self._task("coding", ticket=ticket)
        PullRequest.objects.record_opened(ticket=ticket, url="https://github.com/example-org/example-repo/pull/7")

        assert drain_queue_body()["enqueued"] == [started.pk]
        assert not self._admitted(older)

    def test_started_planning_pull_request_precedes_unstarted_coding_task(self) -> None:
        self._fill_coding_lane()
        unstarted = self._task("coding")
        started_ticket = Ticket.objects.create()
        first = self._task("scoping", status=Task.Status.COMPLETED, ticket=started_ticket)
        Task.objects.filter(pk=first.pk).update(admitted_at=timezone.now() - dt.timedelta(hours=2))
        started = self._task("planning", ticket=started_ticket)
        PullRequest.objects.record_opened(
            ticket=started_ticket, url="https://github.com/example-org/example-repo/pull/13"
        )

        assert drain_queue_body()["enqueued"] == [started.pk]
        assert not self._admitted(unstarted)

    def test_oldest_started_pull_request_precedes_older_task_row(self) -> None:
        self._fill_coding_lane()
        newer_ticket = Ticket.objects.create()
        older_ticket = Ticket.objects.create()
        newer = self._task("coding", ticket=newer_ticket)
        older = self._task("coding", ticket=older_ticket)
        PullRequest.objects.record_opened(ticket=newer_ticket, url="https://github.com/example-org/example-repo/pull/8")
        PullRequest.objects.record_opened(ticket=older_ticket, url="https://github.com/example-org/example-repo/pull/9")
        first = self._task("planning", status=Task.Status.COMPLETED, ticket=older_ticket)
        second = self._task("planning", status=Task.Status.COMPLETED, ticket=newer_ticket)
        Task.objects.filter(pk=first.pk).update(admitted_at=timezone.now() - dt.timedelta(days=3))
        Task.objects.filter(pk=second.pk).update(admitted_at=timezone.now() - dt.timedelta(days=1))

        assert drain_queue_body()["enqueued"] == [older.pk]
        assert not self._admitted(newer)

    def test_oldest_started_pull_request_wins_in_review_lane(self) -> None:
        self._task("shipping", status=Task.Status.CLAIMED)
        newer_ticket = Ticket.objects.create()
        older_ticket = Ticket.objects.create()
        newer = self._task("shipping", ticket=newer_ticket)
        older = self._task("shipping", ticket=older_ticket)
        PullRequest.objects.record_opened(
            ticket=newer_ticket, url="https://github.com/example-org/example-repo/pull/11"
        )
        PullRequest.objects.record_opened(
            ticket=older_ticket, url="https://github.com/example-org/example-repo/pull/12"
        )
        first = self._task("planning", status=Task.Status.COMPLETED, ticket=older_ticket)
        second = self._task("planning", status=Task.Status.COMPLETED, ticket=newer_ticket)
        Task.objects.filter(pk=first.pk).update(admitted_at=timezone.now() - dt.timedelta(days=3))
        Task.objects.filter(pk=second.pk).update(admitted_at=timezone.now() - dt.timedelta(days=1))

        assert drain_queue_body()["enqueued"] == [older.pk]
        assert not self._admitted(newer)

    def test_expedited_unstarted_ticket_keeps_its_priority(self) -> None:
        self._fill_coding_lane()
        started_ticket = Ticket.objects.create()
        started = self._task("coding", ticket=started_ticket)
        PullRequest.objects.record_opened(
            ticket=started_ticket, url="https://github.com/example-org/example-repo/pull/10"
        )
        expedited = self._task("planning", ticket=Ticket.objects.create(expedited=True))

        assert drain_queue_body()["enqueued"] == [expedited.pk]
        assert not self._admitted(started)

    def test_started_review_precedes_pr_less_review_at_the_same_score(self) -> None:
        self._task("reviewing", status=Task.Status.CLAIMED)
        pr_less = self._task("reviewing")
        started_ticket = Ticket.objects.create()
        started = self._task("reviewing", ticket=started_ticket)
        PullRequest.objects.record_opened(
            ticket=started_ticket, url="https://github.com/example-org/example-repo/pull/302"
        )

        assert drain_queue_body()["enqueued"] == [started.pk]
        assert not self._admitted(pr_less)


class TestTheOpenPullRequestStartCap(_AdmissionCase):
    def setUp(self) -> None:
        super().setUp()
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")

    def _open_prs(self, count: int) -> list[Ticket]:
        tickets = [Ticket.objects.create() for _ in range(count)]
        for index, ticket in enumerate(tickets):
            PullRequest.objects.record_opened(
                ticket=ticket, url=f"https://github.com/example-org/example-repo/pull/{index + 40}"
            )
        return tickets

    def test_three_open_prs_hold_new_shipping_and_first_push_but_serve_started_work(self) -> None:
        started_ticket = self._open_prs(3)[0]
        shipping = self._task("shipping")
        first_push = self._task("coding", ticket=Ticket.objects.create())
        started = self._task("coding", ticket=started_ticket)

        assert drain_queue_body()["enqueued"] == [started.pk]
        assert not self._admitted(shipping)
        assert not self._admitted(first_push)

    def test_two_open_prs_allow_a_new_shipping_task(self) -> None:
        self._open_prs(2)
        shipping = self._task("shipping")

        assert drain_queue_body()["enqueued"] == [shipping.pk]

    def test_three_open_prs_allow_review_and_critic(self) -> None:
        self._open_prs(3)
        review = self._task("reviewing")
        critic = self._task("critic_reviewing", ticket=Ticket.objects.create(role=Ticket.Role.REVIEWER))

        assert drain_queue_body()["enqueued"] == [review.pk, critic.pk]

    def test_three_open_prs_allow_expedited_shipping(self) -> None:
        self._open_prs(3)
        expedited = self._task("shipping", ticket=Ticket.objects.create(expedited=True))

        assert drain_queue_body()["enqueued"] == [expedited.pk]

    def test_held_starter_denial_names_the_pull_request_cap(self) -> None:
        self._open_prs(3)
        held = self._task("shipping")

        with self.assertLogs("teatree.core.agent_admission", level="WARNING") as logs:
            assert drain_queue_body()["enqueued"] == []

        assert any("3 or more pull requests are open" in message for message in logs.output)
        assert not self._admitted(held)


class TestAFrozenFactoryAdmitsNothing(_AdmissionCase):
    def setUp(self) -> None:
        super().setUp()
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")

    def test_the_off_posture_withholds_every_waiting_row(self) -> None:
        waiting = self._task("reviewing")
        ModeOverride.objects.set_override("off", reason="test: the operator stopped the fleet")

        assert admit_waiting_tasks(at="test") == []
        assert not self._admitted(waiting)

    def test_control_an_admitting_fleet_admits_the_same_row(self) -> None:
        waiting = self._task("reviewing")

        assert admit_waiting_tasks(at="test") == [waiting.pk]


class TestAnOwnerQuestionHasItsOwnBoundedLane(_AdmissionCase):
    """The owner's Slack question is seated beside coding, never behind it and never in the review seats (#5655)."""

    def _fill_the_coding_lane(self) -> None:
        for _ in range(_CODING_CEILING):
            self._task("coding", status=Task.Status.CLAIMED)

    def test_an_answering_task_is_seated_and_enqueued_while_the_coding_lane_is_full(self) -> None:
        self._fill_the_coding_lane()
        assert not self._admitted(self._task("coding")), "control: the coding lane is really full"

        question = self._task("answering")

        assert self._admitted(question)
        task_dispatch_mod.execute_task.using.assert_called_once_with(queue_name="cheap")

    def test_a_loaded_machine_refuses_coding_but_still_seats_an_owner_question(self) -> None:
        overloaded = MachineSignal(cores=8, load1=8 * 5.0 + 1, ram_available_gb=20.0)
        with patch.object(gate_mod, "read_machine_signal", return_value=overloaded):
            coding = self._task("coding")
            question = self._task("answering")

        assert not self._admitted(coding)
        assert self._admitted(question)

    def test_a_spent_token_budget_still_refuses_an_owner_question(self) -> None:
        spent = QuotaSignal(
            fresh=True,
            all_accounts_exhausted=True,
            weekly_utilization=1.0,
            short_utilization=1.0,
            seconds_to_weekly_reset=100.0,
        )
        with patch.object(gate_mod, "read_quota_signal", return_value=spent):
            question = self._task("answering")

        assert not self._admitted(question)

    def test_a_flood_of_answering_tasks_seats_only_the_answering_width(self) -> None:
        flood = [self._task("answering") for _ in range(ANSWERING_LANE_WIDTH + 3)]

        seated = [self._admitted(task) for task in flood]

        assert seated == [True] * ANSWERING_LANE_WIDTH + [False] * 3

    def test_a_flood_of_answering_tasks_leaves_coding_review_and_retiring_their_seats(self) -> None:
        for _ in range(ANSWERING_LANE_WIDTH + 3):
            self._task("answering")

        coding = [self._task("coding") for _ in range(_CODING_CEILING)]
        reviews = [self._task("reviewing") for _ in range(_REVIEW_WIDTH)]
        shipping = self._task("shipping")

        assert all(self._admitted(task) for task in (*coding, *reviews, shipping))

    def test_full_review_and_retiring_seats_do_not_refuse_an_owner_question(self) -> None:
        self._fill_the_review_lane()
        self._task("shipping", status=Task.Status.CLAIMED)

        assert self._admitted(self._task("answering"))
