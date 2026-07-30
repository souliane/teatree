"""Age-based retention pruning for the high-churn control-DB tables (#3693).

The load-bearing invariant is the safety guard: retention NEVER deletes a row of a
non-terminal ticket, a non-terminal task, or a row within the retention window —
over-deleting a referenced/live row is far worse than a bloated DB. Each guard test
is written to go RED if the guard were dropped (the prunable set would then include
the live row). ``apply_retention`` deletes; ``plan_retention`` reports only.
"""

import datetime as dt

from django.db.models import Min
from django.test import TestCase, override_settings
from django.utils import timezone

from teatree.config.settings import UserSettings
from teatree.core.models import IncomingEvent, Session, Task, TaskAttempt, Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.core.retention.prune import apply_retention, plan_retention

_OLD = timezone.now() - dt.timedelta(days=60)
_RECENT = timezone.now() - dt.timedelta(days=2)
#: Older than the 7-day park window, newer than the 30-day terminal-owned one — the
#: age band that separates the two lanes in a test.
_PARK_AGE = timezone.now() - dt.timedelta(days=10)
#: Comfortably older than every window, so age never masks a closure-keyed assertion.
_ANCIENT = timezone.now() - dt.timedelta(days=120)


def _attempt(
    *,
    ticket_state: str = Ticket.State.MERGED,
    task_status: str = Task.Status.COMPLETED,
    started_at: dt.datetime = _OLD,
    error: str = "",
) -> TaskAttempt:
    ticket = Ticket.objects.create(overlay="acme", state=ticket_state)
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session, status=task_status)
    attempt = TaskAttempt.objects.create(task=task, error=error)
    # started_at is auto_now_add — age it with a direct UPDATE.
    TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=started_at)
    return TaskAttempt.objects.get(pk=attempt.pk)


def _park(
    *,
    ended_at: dt.datetime = _PARK_AGE,
    started_at: dt.datetime | None = None,
    reason: str = "admission: all_accounts_exhausted window on lane 'subscription' active",
    task_status: str = Task.Status.PENDING,
    ticket_state: str = Ticket.State.STARTED,
    **telemetry: object,
) -> TaskAttempt:
    """A park-audit row in the shape ``usage_window._record_park`` writes.

    Defaults mirror the production shape the prune must reach: the owning task is
    back PENDING (a park RETURNS the task to the queue) on a live ticket, which is
    exactly why the terminal-owned lane can never see it.
    """
    ticket = Ticket.objects.create(overlay="acme", state=ticket_state)
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session, status=task_status)
    attempt = TaskAttempt.objects.create(
        task=task,
        exit_code=1,
        error=f"{LIMIT_PARKED_PREFIX}{reason}",
        ended_at=ended_at,
        **telemetry,
    )
    TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=started_at or ended_at)
    return TaskAttempt.objects.get(pk=attempt.pk)


def _event(
    *,
    idempotency_key: str,
    received_at: dt.datetime = _OLD,
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


class TaskAttemptPrunableGuardTestCase(TestCase):
    def test_old_terminal_owned_attempt_is_prunable(self) -> None:
        attempt = _attempt()
        prunable = TaskAttempt.objects.prunable(timezone.now() - dt.timedelta(days=30))
        assert list(prunable.values_list("pk", flat=True)) == [attempt.pk]

    def test_never_prunes_attempt_of_non_terminal_ticket(self) -> None:
        # The critical guard: a live ticket's attempt must survive even when old
        # and its task is terminal — deleting it drops history a live ticket needs.
        _attempt(ticket_state=Ticket.State.STARTED, task_status=Task.Status.COMPLETED)
        prunable = TaskAttempt.objects.prunable(timezone.now() - dt.timedelta(days=30))
        assert prunable.count() == 0

    def test_never_prunes_attempt_of_non_terminal_task(self) -> None:
        _attempt(ticket_state=Ticket.State.MERGED, task_status=Task.Status.PENDING)
        prunable = TaskAttempt.objects.prunable(timezone.now() - dt.timedelta(days=30))
        assert prunable.count() == 0

    def test_never_prunes_attempt_within_window(self) -> None:
        _attempt(started_at=_RECENT)
        prunable = TaskAttempt.objects.prunable(timezone.now() - dt.timedelta(days=30))
        assert prunable.count() == 0

    def test_shipped_ticket_is_not_prunable(self) -> None:
        # SHIPPED is excluded on purpose — its PR is still open and may re-work.
        _attempt(ticket_state=Ticket.State.SHIPPED)
        assert TaskAttempt.objects.prunable(timezone.now() - dt.timedelta(days=30)).count() == 0


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
        # An un-processed, non-dead-lettered event is still in-flight — never pruned.
        _event(idempotency_key="k-inflight", processed=False, dead_lettered=False)
        assert IncomingEvent.objects.prunable(timezone.now() - dt.timedelta(days=30)).count() == 0

    def test_never_prunes_processed_event_within_window(self) -> None:
        _event(idempotency_key="k-recent", received_at=_RECENT)
        assert IncomingEvent.objects.prunable(timezone.now() - dt.timedelta(days=30)).count() == 0


class PlanRetentionTestCase(TestCase):
    def test_plan_reports_without_deleting(self) -> None:
        _attempt()
        _event(idempotency_key="k1")
        plan = plan_retention()
        assert plan.applied is False
        assert plan.total_rows == 2
        assert TaskAttempt.objects.count() == 1
        assert IncomingEvent.objects.count() == 1

    def test_plan_counts_park_junk_subset(self) -> None:
        _attempt(error=f"{LIMIT_PARKED_PREFIX}weekly window exhausted")
        _attempt(error="boom: a genuine crash")
        plan = plan_retention()
        (attempts,) = (t for t in plan.tables if t.table == "TaskAttempt")
        assert attempts.rows == 2
        assert attempts.junk == 1

    def test_zero_window_disables_table(self) -> None:
        _attempt()
        settings = UserSettings(task_attempt_retention_days=0)
        plan = plan_retention(settings=settings)
        (attempts,) = (t for t in plan.tables if t.table == "TaskAttempt")
        assert attempts.disabled is True
        assert attempts.rows == 0


class ApplyRetentionTestCase(TestCase):
    def test_apply_deletes_only_prunable_rows(self) -> None:
        old = _attempt()
        live = _attempt(ticket_state=Ticket.State.STARTED)
        recent = _attempt(started_at=_RECENT)
        old_event = _event(idempotency_key="k-old")
        inflight = _event(idempotency_key="k-inflight", processed=False)

        plan = apply_retention()

        assert plan.applied is True
        assert plan.total_rows == 2
        assert not TaskAttempt.objects.filter(pk=old.pk).exists()
        assert TaskAttempt.objects.filter(pk=live.pk).exists()
        assert TaskAttempt.objects.filter(pk=recent.pk).exists()
        assert not IncomingEvent.objects.filter(pk=old_event.pk).exists()
        assert IncomingEvent.objects.filter(pk=inflight.pk).exists()

    def test_apply_with_zero_window_deletes_nothing(self) -> None:
        _attempt()
        _event(idempotency_key="k1")
        settings = UserSettings(
            task_attempt_retention_days=0, incoming_event_retention_days=0, park_attempt_retention_days=0
        )
        plan = apply_retention(settings=settings)
        assert plan.total_rows == 0
        assert TaskAttempt.objects.count() == 1
        assert IncomingEvent.objects.count() == 1


class ParkLaneReachesWhatTerminalOwnedCannotTestCase(TestCase):
    """The defect the park lane exists to close (#3693 follow-up).

    A park RETURNS its task to the queue PENDING on a live ticket, so
    ``prunable``'s terminal-owned double guard structurally excludes every park
    row: the sanctioned remedy the doctor prescribes for a park-bloated table is
    a guaranteed no-op on exactly the rows that bloated it. These tests pin both
    halves — the terminal-owned lane still cannot see a park, and the park lane can.
    """

    def test_terminal_owned_lane_cannot_reach_a_live_task_park_row(self) -> None:
        _park()
        assert TaskAttempt.objects.prunable(timezone.now() - dt.timedelta(days=30)).count() == 0

    def test_park_lane_reaches_the_live_task_park_row(self) -> None:
        park = _park()
        prunable = TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7))
        assert list(prunable.values_list("pk", flat=True)) == [park.pk]


class ParkPrunableGuardTestCase(TestCase):
    """Each guard goes RED if dropped — the prunable set would then hold the protected row."""

    def test_never_prunes_a_row_without_the_park_marker(self) -> None:
        # The identity key is the ONE canonical marker `usage_window._record_park`
        # writes. A genuine crash of the same age is diagnostic signal, not junk —
        # `stuck_loop:` (the lease-loss breach) is precisely such a row.
        _attempt(error="stuck_loop: lease lost for task 375: re-claimed by another worker", started_at=_PARK_AGE)
        assert TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7)).count() == 0

    def test_never_prunes_a_park_carrying_cost(self) -> None:
        # The 1,203 telemetry-carrying rows ARE the entire cost ledger. Belt-and-braces:
        # the marker alone already implies no telemetry, but a future writer (or a
        # marker-string collision) must not be able to destroy the only cost history.
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
        # #3680 folds a repeated park into ONE row whose `started_at` stays ancient
        # while `ended_at` refreshes each poll. Windowing on `started_at` would delete
        # exactly the row that says "still parked, N polls later" — the live signal
        # `_check_park_spin` and the coalescer both read.
        _park(started_at=timezone.now() - dt.timedelta(days=60), ended_at=timezone.now() - dt.timedelta(hours=1))
        assert TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7)).count() == 0

    def test_falls_back_to_started_at_when_never_ended(self) -> None:
        park = _park()
        TaskAttempt.objects.filter(pk=park.pk).update(ended_at=None)
        prunable = TaskAttempt.objects.prunable_parks(timezone.now() - dt.timedelta(days=7))
        assert list(prunable.values_list("pk", flat=True)) == [park.pk]


class ParkRetentionPlanTestCase(TestCase):
    def test_plan_reports_the_park_lane_separately_from_the_terminal_owned_lane(self) -> None:
        _park()
        _attempt()
        plan = plan_retention()
        (parks,) = (t for t in plan.tables if t.table == "TaskAttempt (park)")
        (attempts,) = (t for t in plan.tables if t.table == "TaskAttempt")
        assert parks.rows == 1
        assert attempts.rows == 1
        assert plan.total_rows == 2
        assert TaskAttempt.objects.count() == 2  # a plan deletes nothing

    def test_zero_park_window_disables_the_park_lane(self) -> None:
        _park()
        plan = plan_retention(settings=UserSettings(park_attempt_retention_days=0))
        (parks,) = (t for t in plan.tables if t.table == "TaskAttempt (park)")
        assert parks.disabled is True
        assert parks.rows == 0


class ParkRetentionApplyTestCase(TestCase):
    def test_apply_deletes_the_park_and_keeps_every_protected_row(self) -> None:
        park = _park()
        recent_park = _park(ended_at=timezone.now() - dt.timedelta(days=1))
        priced_park = _park(cost_usd=0.42)
        crash = _attempt(error="stuck_loop: lease lost for task 375", started_at=_PARK_AGE)

        plan = apply_retention()

        (parks,) = (t for t in plan.tables if t.table == "TaskAttempt (park)")
        assert parks.rows == 1
        assert not TaskAttempt.objects.filter(pk=park.pk).exists()
        assert TaskAttempt.objects.filter(pk=recent_park.pk).exists()
        assert TaskAttempt.objects.filter(pk=priced_park.pk).exists()
        assert TaskAttempt.objects.filter(pk=crash.pk).exists()

    def test_apply_batches_the_delete_so_no_single_statement_spans_the_whole_set(self) -> None:
        # A 330k-row single-statement DELETE holds the SQLite write lock for its whole
        # duration and can collide with a converging deploy. Batching is a correctness
        # requirement of the operational context, so it is pinned, not incidental.
        for _ in range(5):
            _park()
        plan = apply_retention(settings=UserSettings(park_attempt_retention_days=7), batch_size=2)
        (parks,) = (t for t in plan.tables if t.table == "TaskAttempt (park)")
        assert parks.rows == 5
        assert parks.batches == 3
        assert TaskAttempt.objects.count() == 0


_NOOP = (Ticket.State.REVIEW_POSTED, Ticket.State.REVIEW_POSTED, "mark_reviewed_externally")


def _ticket_with_transitions(*, state: str, count: int, move: tuple[str, str, str] = _NOOP) -> Ticket:
    ticket = Ticket.objects.create(overlay="acme", state=state)
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


class TicketTransitionLaneTestCase(TestCase):
    """The transition lane, as ``plan_retention`` / ``apply_retention`` wire it."""

    def test_plan_reports_the_lane_without_a_window(self) -> None:
        _ticket_with_transitions(state=Ticket.State.REVIEW_POSTED, count=4)
        (lane,) = (t for t in plan_retention().tables if t.table == "TicketTransition")
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

    def test_the_kill_switch_disables_the_lane(self) -> None:
        _ticket_with_transitions(state=Ticket.State.MERGED, count=4)
        plan = plan_retention(settings=UserSettings(ticket_transition_prune_disabled=True))
        (lane,) = (t for t in plan.tables if t.table == "TicketTransition")
        assert lane.disabled is True
        assert lane.reason == "ticket_transition_prune_disabled"
        assert TicketTransition.objects.count() == 4

    def test_batching_deletes_the_whole_set(self) -> None:
        _ticket_with_transitions(state=Ticket.State.MERGED, count=8)
        plan = apply_retention(batch_size=2)
        (lane,) = (t for t in plan.tables if t.table == "TicketTransition")
        assert lane.rows == 6
        assert TicketTransition.objects.count() == 2


class ReopenAfterPruneTestCase(TestCase):
    """The deliverable: a pruned ticket can still be reopened with its history intact.

    Anyone can delete rows. What has to hold is that the prune removed only what a
    reopened ticket does not need — so this closes a ticket, prunes, reopens it, and
    asserts every state edge is still there and the FSM still moves.
    """

    def test_a_reopened_ticket_keeps_every_state_edge(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.MERGED)
        edges = [
            TicketTransition.objects.create(ticket=ticket, from_state=src, to_state=dst, triggered_by=name)
            for src, dst, name in (
                (Ticket.State.NOT_STARTED, Ticket.State.STARTED, "start"),
                (Ticket.State.STARTED, Ticket.State.CODED, "code"),
                (Ticket.State.CODED, Ticket.State.MERGED, "reconcile_merged"),
            )
        ]
        for n in range(6):
            row = TicketTransition.objects.create(
                ticket=ticket,
                from_state=Ticket.State.REVIEW_POSTED,
                to_state=Ticket.State.REVIEW_POSTED,
                triggered_by="mark_reviewed_externally",
            )
            TicketTransition.objects.filter(pk=row.pk).update(created_at=_ANCIENT + dt.timedelta(minutes=n))

        apply_retention()

        ticket.reopen()
        ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        surviving_edges = set(ticket.transitions.state_edges().values_list("pk", flat=True))
        assert {edge.pk for edge in edges} <= surviving_edges

    def test_the_prune_leaves_the_creation_proxy_where_it_was(self) -> None:
        """``factory_signal_queries`` dates a fix ticket by ``Min(created_at)``."""
        ticket = _ticket_with_transitions(state=Ticket.State.MERGED, count=5)
        before = ticket.transitions.aggregate(first=Min("created_at"))["first"]

        apply_retention()

        assert ticket.transitions.aggregate(first=Min("created_at"))["first"] == before


#: The library's prune refuses any backend that is not a ``DatabaseBackend``; the
#: suite's default is a dummy one, so the lane's live path needs the real topology.
_DATABASE_BACKEND = {
    "default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]},
}


@override_settings(TASKS=_DATABASE_BACKEND)
class TaskResultLaneTestCase(TestCase):
    def test_plan_reports_the_task_result_lane(self) -> None:
        plan = plan_retention()
        (lane,) = (t for t in plan.tables if t.table == "DBTaskResult")
        assert lane.retention_days == 1
        assert lane.disabled is False

    def test_zero_window_disables_the_task_result_lane(self) -> None:
        settings = UserSettings(task_result_retention_days=0)
        plan = plan_retention(settings=settings)
        (lane,) = (t for t in plan.tables if t.table == "DBTaskResult")
        assert lane.disabled is True
        assert lane.reason == ""


class TaskResultLaneWithoutADatabaseBackendTestCase(TestCase):
    """A non-DB task backend must disable this lane, never abort the whole pass."""

    def test_plan_reports_the_lane_inapplicable(self) -> None:
        (lane,) = (t for t in plan_retention().tables if t.table == "DBTaskResult")
        assert lane.disabled is True
        assert "does not store results in the DB" in lane.reason

    def test_the_other_lanes_still_run(self) -> None:
        _ticket_with_transitions(state=Ticket.State.MERGED, count=4)
        plan = apply_retention()
        (lane,) = (t for t in plan.tables if t.table == "TicketTransition")
        assert lane.rows == 2
