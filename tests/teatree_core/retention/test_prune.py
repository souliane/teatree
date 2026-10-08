"""Control-DB retention: the lane table, the quiescence guards, the batch budget.

The load-bearing invariant is the safety guard: retention NEVER deletes a row of a live
ticket or task, and a ticket's task history goes only once the ticket is finished and has
been quiet past the window. Each guard test names the mutation that turns it red.
``apply_retention`` deletes; ``plan_retention`` reports only.
"""

import datetime as dt
from unittest import mock

from django.db.models import CASCADE, Min, QuerySet
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from teatree.config.settings import UserSettings
from teatree.core.models import (
    AutoReviewDispatch,
    BotPing,
    CriticDispatch,
    DeferredQuestion,
    DeliveryClaim,
    IncomingEvent,
    IntentClassification,
    ReplyDispatch,
    ScannedBroadcast,
    Session,
    Task,
    TaskAttempt,
    Ticket,
)
from teatree.core.models.transition import TicketTransition
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.core.retention import prune
from teatree.core.retention.prune import (
    EVENT_CASCADE_CHILDREN,
    RetentionPlan,
    TableRetention,
    apply_retention,
    plan_retention,
)
from teatree.core.retention.ticket_history import quiescent_tickets, synthetic_loop_umbrella_q
from teatree.utils.url_slug import SYNTHETIC_LOOP_UMBRELLA_URL, is_synthetic_loop_umbrella_url

_NOW = timezone.now()
#: Older than the 56-day task-history floor.
_QUIET = _NOW - dt.timedelta(days=60)
_RECENT = _NOW - dt.timedelta(days=2)
#: Older than the 7-day park window, newer than every other one.
_PARK_AGE = _NOW - dt.timedelta(days=10)
#: Older than the 30-day post-mortem window, newer than the task-history floor.
_PAST_POST_MORTEM = _NOW - dt.timedelta(days=40)
_ANCIENT = _NOW - dt.timedelta(days=120)

_LANE_ORDER = [
    "TaskAttempt (park)",
    "Task (failed)",
    "Task (completed)",
    "BotPing (payload)",
    "IncomingEvent",
    "TicketTransition",
    "DBTaskResult",
]


def _ticket(*, state: str = Ticket.State.MERGED, issue_url: str = "") -> Ticket:
    return Ticket.objects.create(overlay="acme", state=state, issue_url=issue_url)


def _task(
    ticket: Ticket | None = None,
    *,
    status: str = Task.Status.COMPLETED,
    created_at: dt.datetime = _QUIET,
    attempts: int = 1,
    attempt_started_at: dt.datetime | None = None,
) -> Task:
    owner = ticket or _ticket()
    task = Task.objects.create(ticket=owner, session=Session.objects.create(ticket=owner), status=status)
    Task.objects.filter(pk=task.pk).update(created_at=created_at)
    for _ in range(attempts):
        attempt = TaskAttempt.objects.create(task=task)
        TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=attempt_started_at or created_at)
    return task


def _park(
    task: Task | None = None,
    *,
    ended_at: dt.datetime = _PARK_AGE,
    started_at: dt.datetime | None = None,
    **telemetry: object,
) -> TaskAttempt:
    """A park-audit row in the shape ``usage_window.record_park`` writes: by default its task is back PENDING."""
    owner = task or _task(_ticket(state=Ticket.State.WORK_STARTED), status=Task.Status.PENDING, attempts=0)
    attempt = TaskAttempt.objects.create(
        task=owner,
        exit_code=1,
        error=f"{LIMIT_PARKED_PREFIX}admission: all_accounts_exhausted window on lane 'subscription' active",
        ended_at=ended_at,
        **telemetry,
    )
    TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=started_at or ended_at)
    return TaskAttempt.objects.get(pk=attempt.pk)


def _event(
    *,
    idempotency_key: str,
    received_at: dt.datetime = _QUIET,
    processed: bool = True,
    dead_lettered: bool = False,
) -> IncomingEvent:
    return IncomingEvent.objects.create(
        source=IncomingEvent.Source.SLACK,
        idempotency_key=idempotency_key,
        received_at=received_at,
        processed_at=timezone.now() if processed else None,
        dead_lettered_at=timezone.now() if dead_lettered else None,
    )


def _ping(key: str, *, status: str = BotPing.Status.SENT, posted_at: dt.datetime = _PAST_POST_MORTEM) -> BotPing:
    return BotPing.objects.create(
        idempotency_key=key,
        kind=BotPing.Kind.INFO,
        status=status,
        text="the whole notification",
        error_message="a delivery error",
        posted_at=posted_at,
    )


_NOOP = (Ticket.State.REVIEW_DELIVERED, Ticket.State.REVIEW_DELIVERED, "mark_reviewed_externally")


def _ticket_with_transitions(*, state: str, count: int, move: tuple[str, str, str] = _NOOP) -> Ticket:
    ticket = _ticket(state=state)
    from_state, to_state, triggered_by = move
    for n in range(count):
        row = TicketTransition.objects.create(
            ticket=ticket,
            from_state=from_state,
            to_state=to_state,
            triggered_by=triggered_by,
        )
        TicketTransition.objects.filter(pk=row.pk).update(created_at=_ANCIENT + dt.timedelta(minutes=n))
    return ticket


def _lane(plan: RetentionPlan, table: str) -> TableRetention:
    (lane,) = (t for t in plan.tables if t.table == table)
    return lane


def _exists(row: Task | TaskAttempt) -> bool:
    return type(row).objects.filter(pk=row.pk).exists()


class IncomingEventPrunableGuardTestCase(TestCase):
    def test_old_processed_event_is_prunable(self) -> None:
        event = _event(idempotency_key="k-processed")
        prunable = IncomingEvent.objects.prunable(timezone.now() - dt.timedelta(days=30))
        assert list(prunable.values_list("pk", flat=True)) == [event.pk]

    def test_old_dead_lettered_event_is_prunable(self) -> None:
        event = _event(idempotency_key="k-dead", processed=False, dead_lettered=True)
        prunable = IncomingEvent.objects.prunable(timezone.now() - dt.timedelta(days=30))
        assert list(prunable.values_list("pk", flat=True)) == [event.pk]

    def test_never_prunes_old_unprocessed_event(self) -> None:
        _event(idempotency_key="k-inflight", processed=False, dead_lettered=False)
        assert IncomingEvent.objects.prunable(timezone.now() - dt.timedelta(days=30)).count() == 0

    def test_never_prunes_processed_event_within_window(self) -> None:
        _event(idempotency_key="k-recent", received_at=_RECENT)
        assert IncomingEvent.objects.prunable(timezone.now() - dt.timedelta(days=30)).count() == 0


class PlanRetentionTestCase(TestCase):
    def test_plan_reports_without_deleting(self) -> None:
        _task(status=Task.Status.FAILED)
        _event(idempotency_key="k1")
        _ping("ping-1")

        plan = plan_retention()

        assert [table.table for table in plan.tables] == _LANE_ORDER
        assert plan.applied is False
        assert plan.budget_exhausted is False
        assert _lane(plan, "Task (failed)").rows == 1
        assert _lane(plan, "Task (failed)").cascaded == 1
        assert (plan.total_rows, plan.total_compacted) == (2, 1)
        assert {table.max_batch_ms for table in plan.tables} == {0}
        assert Task.objects.count() == 1
        assert TaskAttempt.objects.count() == 1
        assert IncomingEvent.objects.count() == 1
        assert BotPing.objects.get().text == "the whole notification"

    def test_zero_window_disables_table(self) -> None:
        _task()
        plan = plan_retention(settings=UserSettings(task_attempt_retention_days=0))
        for table in ("Task (failed)", "Task (completed)"):
            lane = _lane(plan, table)
            assert lane.disabled is True
            assert lane.rows == 0


class ApplyRetentionTestCase(TestCase):
    def test_apply_deletes_only_prunable_rows(self) -> None:
        old = _task()
        live = _task(_ticket(state=Ticket.State.WORK_STARTED))
        recent = _task(created_at=_RECENT)
        old_event = _event(idempotency_key="k-old")
        inflight = _event(idempotency_key="k-inflight", processed=False)

        plan = apply_retention()

        assert plan.applied is True
        assert plan.total_rows == 2
        assert not _exists(old)
        assert _exists(live)
        assert _exists(recent)
        assert not IncomingEvent.objects.filter(pk=old_event.pk).exists()
        assert IncomingEvent.objects.filter(pk=inflight.pk).exists()

    def test_apply_with_zero_window_deletes_nothing(self) -> None:
        task = _task()
        plan = apply_retention(settings=UserSettings(task_attempt_retention_days=0))
        assert plan.total_rows == 0
        assert _exists(task)
        assert TaskAttempt.objects.count() == 1

    def test_apply_removes_exactly_what_the_dry_run_counted(self) -> None:
        ticket = _ticket()
        failed = _task(ticket, status=Task.Status.FAILED)
        stale_park = _park(failed, started_at=_QUIET)
        completed = _task(ticket, attempts=2)
        _ping("ping-1")
        _event(idempotency_key="k-old")
        _ticket_with_transitions(state=Ticket.State.MERGED, count=3)

        planned = plan_retention()
        applied = apply_retention()

        assert [(t.table, t.rows, t.cascaded, t.compacted) for t in applied.tables] == [
            (t.table, t.rows, t.cascaded, t.compacted) for t in planned.tables
        ]
        assert _lane(planned, "TaskAttempt (park)").rows == 1
        assert _lane(planned, "Task (failed)").cascaded == 1
        assert _lane(planned, "Task (completed)").cascaded == 2
        assert not _exists(failed)
        assert not _exists(completed)
        assert not _exists(stale_park)
        assert TaskAttempt.objects.count() == 0
        assert applied.budget_exhausted is False

    def test_an_event_with_cascade_children_counts_the_same_in_plan_and_apply(self) -> None:
        event = _event(idempotency_key="k-classified")
        IntentClassification.objects.create(event=event, intent=IntentClassification.Intent.NOISE, confidence=0.5)
        ReplyDispatch.objects.create(event=event, target_ref="C1", action_name="reply", idempotency_key="rd-1")

        planned = _lane(plan_retention(), "IncomingEvent")
        applied = _lane(apply_retention(), "IncomingEvent")

        assert (applied.rows, applied.cascaded) == (planned.rows, planned.cascaded) == (1, 2)
        assert not IntentClassification.objects.exists()
        assert not ReplyDispatch.objects.exists()

    def test_the_counted_event_children_are_every_cascade_child_of_the_model(self) -> None:
        cascading = {rel.related_model for rel in IncomingEvent._meta.related_objects if rel.on_delete is CASCADE}
        assert cascading == set(EVENT_CASCADE_CHILDREN)


class RetentionPlanCountsTestCase(SimpleTestCase):
    def test_counts_reports_each_lane_and_the_pass_totals(self) -> None:
        plan = RetentionPlan(
            _NOW,
            (
                TableRetention("Task (failed)", 56, 3, cascaded=2, max_batch_ms=40),
                TableRetention("BotPing (payload)", 30, 0, compacted=5, max_batch_ms=90),
            ),
            applied=True,
            budget_exhausted=True,
        )
        assert plan.counts() == {
            "Task (failed)": 3,
            "BotPing (payload)": 5,
            "cascaded": 2,
            "max_batch_ms": 90,
            "budget_exhausted": 1,
        }


class TaskHistoryLaneTestCase(TestCase):
    """A ticket's task history goes only when the ticket is finished and nothing about it moved."""

    def test_quiet_finished_ticket_loses_its_tasks_and_attempts(self) -> None:
        task = _task(attempts=3)
        apply_retention()
        assert not _exists(task)
        assert TaskAttempt.objects.count() == 0

    def test_recent_task_keeps_the_ticket(self) -> None:
        ticket = _ticket()
        old = _task(ticket)
        _task(ticket, created_at=_RECENT, attempts=0)
        apply_retention()
        assert _exists(old)

    def test_recent_attempt_on_an_old_task_keeps_the_ticket(self) -> None:
        ticket = _ticket()
        old = _task(ticket)
        _task(ticket, attempt_started_at=_RECENT)
        apply_retention()
        assert _exists(old)

    def test_recent_transition_keeps_the_ticket(self) -> None:
        ticket = _ticket()
        old = _task(ticket)
        TicketTransition.objects.create(
            ticket=ticket,
            from_state=Ticket.State.REVIEW_DELIVERED,
            to_state=Ticket.State.REVIEW_DELIVERED,
            triggered_by="mark_reviewed_externally",
        )
        apply_retention()
        assert _exists(old)

    def test_never_prunes_a_non_terminal_ticket(self) -> None:
        for state in (Ticket.State.WORK_STARTED, Ticket.State.PR_OPENED):
            with self.subTest(state=state):
                task = _task(_ticket(state=state))
                apply_retention()
                assert _exists(task)

    def test_old_pending_task_keeps_its_terminal_sibling(self) -> None:
        ticket = _ticket()
        _task(ticket, status=Task.Status.PENDING, attempts=0)
        sibling = _task(ticket)
        apply_retention()
        assert _exists(sibling)

    def test_claimed_task_keeps_its_terminal_sibling(self) -> None:
        ticket = _ticket()
        _task(ticket, status=Task.Status.CLAIMED, attempts=0)
        sibling = _task(ticket)
        apply_retention()
        assert _exists(sibling)

    def test_a_task_that_stops_qualifying_before_the_delete_is_kept(self) -> None:
        task = _task()
        select = prune._next_batch

        def select_then_reactivate(rows: QuerySet) -> list[int]:
            batch = select(rows)
            if rows.model is Task and batch:
                TaskAttempt.objects.create(task_id=batch[0])
            return batch

        with mock.patch.object(prune, "_next_batch", select_then_reactivate):
            plan = apply_retention()

        assert _exists(task)
        assert _lane(plan, "Task (completed)").rows == 0

    def test_synthetic_ticket_is_kept_and_a_lookalike_url_is_not(self) -> None:
        synthetic = _task(_ticket(issue_url=f"{SYNTHETIC_LOOP_UMBRELLA_URL}#directive=7"))
        bare = _task(_ticket(issue_url=SYNTHETIC_LOOP_UMBRELLA_URL))
        lookalike = _task(_ticket(issue_url=f"{SYNTHETIC_LOOP_UMBRELLA_URL}1"))

        apply_retention()

        assert _exists(synthetic)
        assert _exists(bare)
        assert not _exists(lookalike)

    def test_the_synthetic_q_agrees_with_the_python_predicate(self) -> None:
        urls = [
            SYNTHETIC_LOOP_UMBRELLA_URL,
            f"{SYNTHETIC_LOOP_UMBRELLA_URL}#outer-loop-experiment=3",
            f"{SYNTHETIC_LOOP_UMBRELLA_URL}1",
            "https://github.com/acme/widgets/issues/3009",
        ]
        for url in urls:
            _ticket(issue_url=url)
        matched = set(Ticket.objects.filter(synthetic_loop_umbrella_q("issue_url")).values_list("issue_url", flat=True))
        assert matched == {url for url in urls if is_synthetic_loop_umbrella_url(url)}

    def test_pending_broadcast_reviewer_task_keeps_the_ticket(self) -> None:
        reviewer = _task()
        merged_reviewer = _task()
        for task, classification in (
            (reviewer, ScannedBroadcast.Classification.PENDING),
            (merged_reviewer, ScannedBroadcast.Classification.ALL_MERGED),
        ):
            ScannedBroadcast.objects.create(
                channel="C1",
                slack_ts=f"1.{task.pk}",
                classification=classification,
                reviewer_task_id=str(task.pk),
            )

        apply_retention()

        assert _exists(reviewer)
        assert not _exists(merged_reviewer)

    def test_open_parked_question_keeps_the_ticket(self) -> None:
        parked = _task()
        answered = _task()
        DeferredQuestion.objects.create(question="which way?", parked_task=parked)
        DeferredQuestion.objects.create(question="which way?", parked_task=answered, answered_at=_RECENT)

        apply_retention()

        assert _exists(parked)
        assert not _exists(answered)

    def test_window_never_drops_below_the_factory_lookback(self) -> None:
        inside = _task(created_at=_NOW - dt.timedelta(days=40))
        outside = _task(created_at=_QUIET)

        apply_retention(settings=UserSettings(task_attempt_retention_days=30))

        assert _exists(inside)
        assert not _exists(outside)

    def test_pruning_a_task_keeps_dispatch_and_question_ledger_rows(self) -> None:
        task = _task()
        review = AutoReviewDispatch.objects.create(slug="acme/widgets", pr_id=1, head_sha="abc", task=task)
        critic = CriticDispatch.objects.create(ticket=task.ticket, transition="mark_merged", task=task)
        question = DeferredQuestion.objects.create(question="q", parked_task=task, answered_at=_QUIET)

        apply_retention()

        assert not _exists(task)
        for row in (review, critic, question):
            row.refresh_from_db()
        assert (review.task_id, critic.task_id, question.parked_task_id) == (None, None, None)


class BudgetTestCase(TestCase):
    def test_a_capped_run_spends_its_budget_on_failures_first_and_says_it_stopped(self) -> None:
        completed = _task()
        failed = _task(status=Task.Status.FAILED)

        with mock.patch.object(prune, "BATCH_SIZE", 1):
            plan = apply_retention(max_batches=1)

        assert not _exists(failed)
        assert _exists(completed)
        assert plan.budget_exhausted is True

    def test_task_batch_stops_at_the_attempt_cap(self) -> None:
        first = _task(attempts=2)
        rest = [_task(), _task()]

        with mock.patch.object(prune, "TASK_BATCH_MAX_ATTEMPTS", 2):
            plan = apply_retention(max_batches=1)

        lane = _lane(plan, "Task (completed)")
        assert (lane.rows, lane.cascaded, lane.batches) == (1, 2, 1)
        assert not _exists(first)
        assert all(_exists(task) for task in rest)

    def test_a_task_over_the_attempt_cap_still_goes_alone(self) -> None:
        task = _task(attempts=3)

        with mock.patch.object(prune, "TASK_BATCH_MAX_ATTEMPTS", 2):
            plan = apply_retention()

        lane = _lane(plan, "Task (completed)")
        assert (lane.rows, lane.cascaded) == (1, 3)
        assert not _exists(task)

    def test_a_drained_run_reports_no_exhaustion(self) -> None:
        _task()
        with mock.patch.object(prune, "BATCH_SIZE", 1):
            plan = apply_retention(max_batches=1)
        assert plan.budget_exhausted is False


class BotPingPayloadLaneTestCase(TestCase):
    def test_old_sent_ping_loses_its_payload_and_still_dedupes(self) -> None:
        _ping("pr-sweep-flag:42:conflict")

        plan = apply_retention()

        row = BotPing.objects.get(idempotency_key="pr-sweep-flag:42:conflict")
        assert (row.text, row.error_message, row.status) == ("", "", BotPing.Status.SENT)
        assert _lane(plan, "BotPing (payload)").compacted == 1
        assert _lane(plan, "BotPing (payload)").rows == 0
        claim = BotPing.claim_delivery("pr-sweep-flag:42:conflict", kind=BotPing.Kind.INFO, text="again")
        assert claim == DeliveryClaim.ALREADY_SENT

    def test_redeliverable_or_sending_ping_keeps_its_payload(self) -> None:
        for status in (BotPing.Status.FAILED, BotPing.Status.NOOP, BotPing.Status.SENDING):
            _ping(f"k-{status}", status=status)

        apply_retention()

        assert set(BotPing.objects.values_list("text", flat=True)) == {"the whole notification"}

    def test_ping_inside_the_window_keeps_its_payload(self) -> None:
        _ping("recent", posted_at=_RECENT)
        apply_retention()
        assert BotPing.objects.get().text == "the whole notification"

    def test_second_run_compacts_nothing(self) -> None:
        _ping("ping-1")
        apply_retention()
        assert _lane(apply_retention(), "BotPing (payload)").compacted == 0


class ParkLaneReachesWhatTheTaskLanesCannotTestCase(TestCase):
    """A park RETURNS its task to the queue PENDING, so no ticket-keyed lane can ever reach it."""

    def test_task_lanes_cannot_reach_a_live_task_park_row(self) -> None:
        park = _park()
        assert not quiescent_tickets(_NOW - dt.timedelta(days=56)).filter(pk=park.task.ticket_id).exists()

    def test_park_lane_reaches_the_live_task_park_row(self) -> None:
        park = _park()
        prunable = TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7))
        assert list(prunable.values_list("pk", flat=True)) == [park.pk]


class ParkPrunableGuardTestCase(TestCase):
    """Each guard goes RED if dropped — the prunable set would then hold the protected row."""

    def test_never_prunes_a_row_without_the_park_marker(self) -> None:
        # A `stuck_loop:` lease-loss breach is diagnostic signal, not park junk.
        task = _task(_ticket(state=Ticket.State.WORK_STARTED), attempts=0)
        TaskAttempt.objects.create(task=task, error="stuck_loop: lease lost for task 375", ended_at=_PARK_AGE)
        assert TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7)).count() == 0

    def test_never_prunes_a_park_carrying_cost(self) -> None:
        _park(cost_usd=1.23)
        assert TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7)).count() == 0

    def test_never_prunes_a_park_carrying_output_tokens(self) -> None:
        _park(output_tokens=4096)
        assert TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7)).count() == 0

    def test_never_prunes_a_park_carrying_cache_tokens(self) -> None:
        _park(cache_read_tokens=128)
        assert TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7)).count() == 0

    def test_never_prunes_a_park_within_the_window(self) -> None:
        _park(ended_at=timezone.now() - dt.timedelta(days=1))
        assert TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7)).count() == 0

    def test_window_is_measured_on_the_last_observation_not_the_first(self) -> None:
        # A repeated park folds into ONE row whose `ended_at` refreshes each poll.
        _park(started_at=timezone.now() - dt.timedelta(days=60), ended_at=timezone.now() - dt.timedelta(hours=1))
        assert TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7)).count() == 0

    def test_falls_back_to_started_at_when_never_ended(self) -> None:
        park = _park()
        TaskAttempt.objects.filter(pk=park.pk).update(ended_at=None)
        prunable = TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7))
        assert list(prunable.values_list("pk", flat=True)) == [park.pk]


class ParkRetentionTestCase(TestCase):
    def test_plan_reports_the_park_lane_separately_from_the_task_lanes(self) -> None:
        _park()
        _task()
        plan = plan_retention()
        assert _lane(plan, "TaskAttempt (park)").rows == 1
        assert _lane(plan, "Task (completed)").rows == 1
        assert TaskAttempt.objects.count() == 2

    def test_apply_deletes_the_park_and_keeps_every_protected_row(self) -> None:
        park = _park()
        recent_park = _park(ended_at=timezone.now() - dt.timedelta(days=1))
        priced_park = _park(cost_usd=0.42)

        plan = apply_retention()

        assert _lane(plan, "TaskAttempt (park)").rows == 1
        assert not _exists(park)
        assert _exists(recent_park)
        assert _exists(priced_park)

    def test_apply_batches_the_delete_so_no_single_statement_spans_the_whole_set(self) -> None:
        for _ in range(5):
            _park()
        with mock.patch.object(prune, "BATCH_SIZE", 2):
            plan = apply_retention()
        parks = _lane(plan, "TaskAttempt (park)")
        assert (parks.rows, parks.batches) == (5, 3)
        assert TaskAttempt.objects.count() == 0


class TicketTransitionLaneTestCase(TestCase):
    def test_plan_reports_the_lane_without_a_window(self) -> None:
        _ticket_with_transitions(state=Ticket.State.REVIEW_DELIVERED, count=4)
        lane = _lane(plan_retention(), "TicketTransition")
        assert lane.rows == 2
        assert lane.aged is False

    def test_apply_keeps_an_open_tickets_trail(self) -> None:
        live = _ticket_with_transitions(state=Ticket.State.CODED, count=4)
        closed = _ticket_with_transitions(state=Ticket.State.MERGED, count=4)

        apply_retention()

        assert live.transitions.count() == 4
        assert closed.transitions.count() == 2

    def test_apply_is_idempotent(self) -> None:
        _ticket_with_transitions(state=Ticket.State.MERGED, count=5)
        first = apply_retention()
        second = apply_retention()
        assert first.total_rows == 3
        assert second.total_rows == 0

    def test_batching_deletes_the_whole_set(self) -> None:
        _ticket_with_transitions(state=Ticket.State.MERGED, count=8)
        with mock.patch.object(prune, "BATCH_SIZE", 2):
            plan = apply_retention()
        assert _lane(plan, "TicketTransition").rows == 6
        assert TicketTransition.objects.count() == 2


class ReopenAfterPruneTestCase(TestCase):
    """A pruned ticket can still be reopened with every state edge intact."""

    def test_a_reopened_ticket_keeps_every_state_edge(self) -> None:
        ticket = _ticket()
        edges = [
            TicketTransition.objects.create(ticket=ticket, from_state=src, to_state=dst, triggered_by=name)
            for src, dst, name in (
                (Ticket.State.NOT_STARTED, Ticket.State.WORK_STARTED, "start"),
                (Ticket.State.WORK_STARTED, Ticket.State.CODED, "code"),
                (Ticket.State.CODED, Ticket.State.MERGED, "reconcile_merged"),
            )
        ]
        for n in range(6):
            row = TicketTransition.objects.create(
                ticket=ticket,
                from_state=Ticket.State.REVIEW_DELIVERED,
                to_state=Ticket.State.REVIEW_DELIVERED,
                triggered_by="mark_reviewed_externally",
            )
            TicketTransition.objects.filter(pk=row.pk).update(created_at=_ANCIENT + dt.timedelta(minutes=n))

        apply_retention()

        ticket.reopen()
        ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
        surviving_edges = set(ticket.transitions.state_edges().values_list("pk", flat=True))
        assert {edge.pk for edge in edges} <= surviving_edges

    def test_the_prune_leaves_the_creation_proxy_where_it_was(self) -> None:
        """``factory_signal_queries`` dates a fix ticket by ``Min(created_at)``."""
        ticket = _ticket_with_transitions(state=Ticket.State.MERGED, count=5)
        before = ticket.transitions.aggregate(first=Min("created_at"))["first"]

        apply_retention()

        assert ticket.transitions.aggregate(first=Min("created_at"))["first"] == before


#: The library's prune refuses any backend that is not a ``DatabaseBackend``.
_DATABASE_BACKEND = {
    "default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]},
}


@override_settings(TASKS=_DATABASE_BACKEND)
class TaskResultLaneTestCase(TestCase):
    def test_plan_reports_the_task_result_lane(self) -> None:
        lane = _lane(plan_retention(), "DBTaskResult")
        assert lane.retention_days == 1
        assert lane.disabled is False

    def test_zero_window_disables_the_task_result_lane(self) -> None:
        lane = _lane(plan_retention(settings=UserSettings(task_result_retention_days=0)), "DBTaskResult")
        assert lane.disabled is True
        assert lane.reason == ""


class TaskResultLaneWithoutADatabaseBackendTestCase(TestCase):
    """A non-DB task backend must disable this lane, never abort the whole pass."""

    def test_plan_reports_the_lane_inapplicable(self) -> None:
        lane = _lane(plan_retention(), "DBTaskResult")
        assert lane.disabled is True
        assert "does not store results in the DB" in lane.reason

    def test_the_other_lanes_still_run(self) -> None:
        _ticket_with_transitions(state=Ticket.State.MERGED, count=4)
        assert _lane(apply_retention(), "TicketTransition").rows == 2
