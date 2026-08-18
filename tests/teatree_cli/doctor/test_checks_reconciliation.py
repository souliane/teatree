"""The continuous runtime reconciliation ledger (Plan-2 Wave B).

Each end-to-end invariant check goes ``alarm`` against a seeded violating DB
state and ``ok`` against a clean one — the pair pins the threshold so neither a
check that always alarms nor one that never alarms can pass. The notify wiring
DMs each alarm under a per-day idempotency key (never an ``ok`` finding), and the
doctor hook surfaces findings without reddening the exit code.
"""

import datetime as dt
import io
from contextlib import redirect_stdout
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from teatree.cli.doctor import checks_external_outcomes as external
from teatree.cli.doctor import checks_reconciliation as recon
from teatree.cli.doctor.checks_reconciliation import reconcile_and_notify, run_reconciliation_checks
from teatree.core.factory.external_outcomes import DEFAULT_EXTERNAL_WINDOW_DAYS, Forge
from teatree.core.models import (
    AutoReviewDispatch,
    CodexReviewMarker,
    DeferredQuestion,
    EvalRunRecord,
    ExternalOutcomeSnapshot,
    IncomingEvent,
    Loop,
    LoopState,
    Mode,
    ModeOverride,
    ReviewVerdict,
    Session,
    Task,
    TaskAttempt,
    Ticket,
)
from teatree.core.models.auto_review_dispatch import MAX_DISPATCH_ATTEMPTS
from teatree.core.models.eval_run import EvalVerdict
from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.transition import TicketTransition
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.loops.loop_staleness import freeze_cutoff_seconds


def _attempt(
    ticket: Ticket, *, cost: float | None = None, error: str = "", outcome_success: bool = False
) -> TaskAttempt:
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)
    kwargs: dict[str, object] = {"task": task, "error": error}
    if cost is not None:
        kwargs["cost_usd"] = cost
    if outcome_success:
        kwargs["exit_code"] = 0
    return TaskAttempt.objects.create(**kwargs)


def _age_attempt(attempt: TaskAttempt, *, started_at: dt.datetime) -> None:
    # ``started_at`` is auto_now_add — bypass it with a direct UPDATE to age a row.
    TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=started_at)


class ParkSpinTestCase(TestCase):
    def test_no_park_rows_is_ok(self) -> None:
        finding = recon._check_park_spin()
        assert finding.level == "ok"

    def test_park_burst_over_threshold_alarms(self) -> None:
        ticket = Ticket.objects.create()
        with patch.object(recon, "MAX_PARK_ROWS_PER_DAY", 2):
            for _ in range(3):
                _attempt(ticket, error=f"{LIMIT_PARKED_PREFIX}weekly window exhausted")
            finding = recon._check_park_spin()
        assert finding.is_alarm
        assert "Park-spin" in finding.message
        assert "`3`" in finding.message

    def test_at_threshold_is_ok(self) -> None:
        ticket = Ticket.objects.create()
        with patch.object(recon, "MAX_PARK_ROWS_PER_DAY", 2):
            for _ in range(2):
                _attempt(ticket, error=f"{LIMIT_PARKED_PREFIX}window")
            finding = recon._check_park_spin()
        assert finding.level == "ok"

    def test_non_park_error_not_counted(self) -> None:
        ticket = Ticket.objects.create()
        with patch.object(recon, "MAX_PARK_ROWS_PER_DAY", 0):
            _attempt(ticket, error="boom: a genuine crash")
            finding = recon._check_park_spin()
        assert finding.level == "ok"

    def test_park_row_older_than_24h_excluded(self) -> None:
        ticket = Ticket.objects.create()
        with patch.object(recon, "MAX_PARK_ROWS_PER_DAY", 0):
            old = _attempt(ticket, error=f"{LIMIT_PARKED_PREFIX}window")
            _age_attempt(old, started_at=timezone.now() - dt.timedelta(hours=25))
            finding = recon._check_park_spin()
        assert finding.level == "ok"

    def test_a_spin_that_coalesces_into_one_row_still_alarms(self) -> None:
        """The detector counts park OBSERVATIONS; coalescing must not blind it.

        A poller re-deriving one unchanged reason now updates a single row rather than
        appending per poll, so a row count alone would read a 231k-poll spin as one row.
        """
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        with patch.object(recon, "MAX_PARK_ROWS_PER_DAY", 2):
            for _ in range(6):
                TaskAttempt.objects.create(task=task, error=f"{LIMIT_PARKED_PREFIX}window")
            finding = recon._check_park_spin()

        assert TaskAttempt.objects.count() == 1, "the coalescing under test must actually be in play"
        assert finding.is_alarm
        assert "`6`" in finding.message


class CostPerDeliveredTicketTestCase(TestCase):
    def test_no_spend_is_ok(self) -> None:
        assert recon._check_cost_per_delivered_ticket().level == "ok"

    def test_spend_with_zero_deliveries_alarms(self) -> None:
        ticket = Ticket.objects.create()
        _attempt(ticket, cost=42.0)
        finding = recon._check_cost_per_delivered_ticket()
        assert finding.is_alarm
        assert "`0`" in finding.message
        assert "$42.00" in finding.message

    def test_per_ticket_over_floor_alarms(self) -> None:
        Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.DELIVERED)
        billed = Ticket.objects.create()
        _attempt(billed, cost=100.0)
        with patch.object(recon, "MAX_USD_PER_DELIVERED_TICKET", 50.0):
            finding = recon._check_cost_per_delivered_ticket()
        assert finding.is_alarm
        assert "$100.00" in finding.message
        assert "per author-delivered ticket" in finding.message

    def test_per_ticket_under_floor_is_ok(self) -> None:
        Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.DELIVERED)
        billed = Ticket.objects.create()
        _attempt(billed, cost=10.0)
        with patch.object(recon, "MAX_USD_PER_DELIVERED_TICKET", 50.0):
            finding = recon._check_cost_per_delivered_ticket()
        assert finding.level == "ok"

    def test_merged_author_ticket_counts_as_delivered(self) -> None:
        Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.MERGED)
        billed = Ticket.objects.create()
        _attempt(billed, cost=10.0)
        with patch.object(recon, "MAX_USD_PER_DELIVERED_TICKET", 50.0):
            assert recon._check_cost_per_delivered_ticket().level == "ok"

    def test_old_spend_still_counted(self) -> None:
        """This ratio is deliberately unwindowed — windowing it would defang it, not unstick it.

        Only the numerator could be windowed (``Ticket`` carries no timestamp), and
        windowed spend over lifetime deliveries trends to zero: a check that can
        never fire is worse than one that fires late.
        """
        Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.DELIVERED)
        billed = Ticket.objects.create()
        _age_attempt(_attempt(billed, cost=100.0), started_at=timezone.now() - dt.timedelta(days=400))
        with patch.object(recon, "MAX_USD_PER_DELIVERED_TICKET", 50.0):
            assert recon._check_cost_per_delivered_ticket().is_alarm

    def test_alarm_clears_as_deliveries_accumulate(self) -> None:
        """Not a ratchet: the denominator grows too, so cheap deliveries drag the average back under."""
        runaway = Ticket.objects.create()
        _attempt(runaway, cost=300.0)
        Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.DELIVERED)
        with patch.object(recon, "MAX_USD_PER_DELIVERED_TICKET", 50.0):
            assert recon._check_cost_per_delivered_ticket().is_alarm
            for _ in range(6):
                _attempt(Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.DELIVERED), cost=1.0)
            assert recon._check_cost_per_delivered_ticket().level == "ok"


class DeadTicketSpendTestCase(TestCase):
    def test_no_dead_spend_is_ok(self) -> None:
        assert recon._check_dead_ticket_spend().level == "ok"

    def test_spend_on_ignored_ticket_over_floor_alarms(self) -> None:
        ignored = Ticket.objects.create(state=Ticket.State.IGNORED)
        _attempt(ignored, cost=100.0)
        with patch.object(recon, "MAX_USD_ON_DEAD_TICKETS", 50.0):
            finding = recon._check_dead_ticket_spend()
        assert finding.is_alarm
        assert "Dead-ticket-spend" in finding.message
        assert "$100.00" in finding.message

    def test_spend_on_delivered_ticket_not_counted(self) -> None:
        delivered = Ticket.objects.create(state=Ticket.State.DELIVERED)
        _attempt(delivered, cost=500.0)
        with patch.object(recon, "MAX_USD_ON_DEAD_TICKETS", 50.0):
            assert recon._check_dead_ticket_spend().level == "ok"

    def test_spend_older_than_the_window_excluded(self) -> None:
        """Sunk history must age out, or the alarm is a ratchet that can never clear.

        Summing every attempt ever means one past runaway pins the check red
        permanently — no action clears it, and a doctor with a stuck red light
        trains the operator to ignore the ones that are still actionable.
        """
        ignored = Ticket.objects.create(state=Ticket.State.IGNORED)
        _age_attempt(
            _attempt(ignored, cost=100.0),
            started_at=timezone.now() - recon._DEAD_SPEND_WINDOW - dt.timedelta(hours=1),
        )
        with patch.object(recon, "MAX_USD_ON_DEAD_TICKETS", 50.0):
            assert recon._check_dead_ticket_spend().level == "ok"

    def test_spend_inside_the_window_still_alarms(self) -> None:
        """The window must not defang the check — recent waste still has to fire."""
        ignored = Ticket.objects.create(state=Ticket.State.IGNORED)
        _age_attempt(
            _attempt(ignored, cost=100.0),
            started_at=timezone.now() - recon._DEAD_SPEND_WINDOW + dt.timedelta(hours=1),
        )
        with patch.object(recon, "MAX_USD_ON_DEAD_TICKETS", 50.0):
            assert recon._check_dead_ticket_spend().is_alarm

    def test_window_is_anchored_on_the_injected_now(self) -> None:
        """``now`` is the dispatcher's clock — a check that ignores it cannot be tested deterministically."""
        ignored = Ticket.objects.create(state=Ticket.State.IGNORED)
        _age_attempt(_attempt(ignored, cost=100.0), started_at=timezone.now() - dt.timedelta(days=2))
        with patch.object(recon, "MAX_USD_ON_DEAD_TICKETS", 50.0):
            future = timezone.now() + recon._DEAD_SPEND_WINDOW
            assert recon._check_dead_ticket_spend(now=future).level == "ok"


class EnabledLoopsTickedTestCase(TestCase):
    def setUp(self) -> None:
        # The default loops are seeded into the control DB; clear them so each
        # test drives the freeze check from a known loop set.
        Loop.objects.all().delete()

    def test_no_loops_is_ok(self) -> None:
        assert recon._check_enabled_loops_ticked().level == "ok"

    def test_enabled_loop_never_ticked_alarms(self) -> None:
        Loop.objects.create(name="probe", script="src/teatree/loops/probe/loop.py", delay_seconds=60, enabled=True)
        finding = recon._check_enabled_loops_ticked()
        assert finding.is_alarm
        assert "`probe`" in finding.message
        assert "Loop-freeze" in finding.message

    def test_recently_ticked_loop_is_ok(self) -> None:
        Loop.objects.create(
            name="probe",
            script="src/teatree/loops/probe/loop.py",
            delay_seconds=60,
            enabled=True,
            last_run_at=timezone.now() - dt.timedelta(hours=1),
        )
        assert recon._check_enabled_loops_ticked().level == "ok"

    def test_disabled_stale_loop_not_alarmed(self) -> None:
        Loop.objects.create(name="probe", script="src/teatree/loops/probe/loop.py", delay_seconds=60, enabled=False)
        assert recon._check_enabled_loops_ticked().level == "ok"

    def test_stale_tick_over_24h_alarms(self) -> None:
        Loop.objects.create(
            name="probe",
            script="src/teatree/loops/probe/loop.py",
            delay_seconds=60,
            enabled=True,
            last_run_at=timezone.now() - dt.timedelta(hours=25),
        )
        assert recon._check_enabled_loops_ticked().is_alarm


class FreezeCutoffScalesWithCadenceTestCase(TestCase):
    """The cutoff is the loop's OWN cadence, not a flat day (#4355).

    A weekly loop was "stale" by the flat rule for ~86% of every week, so the alarm
    naming a genuinely dead daily loop arrived beside a permanent false one.
    """

    def setUp(self) -> None:
        Loop.objects.all().delete()

    def _seed(self, *, cadence_seconds: int, ran_days_ago: int) -> None:
        Loop.objects.create(
            name="probe",
            script="src/teatree/loops/probe/loop.py",
            delay_seconds=cadence_seconds,
            enabled=True,
            last_run_at=timezone.now() - dt.timedelta(days=ran_days_ago),
        )

    def test_a_weekly_loop_three_days_behind_is_not_stale(self) -> None:
        self._seed(cadence_seconds=604800, ran_days_ago=3)
        assert recon._check_enabled_loops_ticked().level == "ok"

    def test_a_daily_loop_three_days_behind_is_stale(self) -> None:
        self._seed(cadence_seconds=86400, ran_days_ago=3)
        assert recon._check_enabled_loops_ticked().is_alarm

    def test_a_weekly_loop_a_month_behind_is_still_caught(self) -> None:
        self._seed(cadence_seconds=604800, ran_days_ago=30)
        assert recon._check_enabled_loops_ticked().is_alarm

    def test_an_anchor_exactly_at_the_cutoff_is_stale(self) -> None:
        # Injected clock, so the boundary is the assertion rather than the drift between
        # seeding and reading: three missed slots IS a stopped loop, per the multiplier.
        moment = timezone.now()
        cutoff = dt.timedelta(seconds=freeze_cutoff_seconds(86400))
        Loop.objects.create(
            name="probe",
            script="src/teatree/loops/probe/loop.py",
            delay_seconds=86400,
            enabled=True,
            last_run_at=moment - cutoff,
        )
        assert recon._check_enabled_loops_ticked(now=moment).is_alarm


class FrozenIsNotWithheldTestCase(TestCase):
    """Two states, two causes, two remedies — the alarm renders them apart (#4355).

    FROZEN is "no tick happened"; WITHHELD is "ticks happen and every pass declines to
    advance the cadence anchor". Sending a reader after a driver that is running, or
    after a gate that is not refusing, is the cost of conflating them.
    """

    def setUp(self) -> None:
        Loop.objects.all().delete()

    def _seed(self, name: str, *, attempted_at: "dt.datetime | None") -> None:
        Loop.objects.create(
            name=name,
            script=f"src/teatree/loops/{name}/loop.py",
            delay_seconds=86400,
            enabled=True,
            last_run_at=timezone.now() - dt.timedelta(days=7),
            last_attempt_at=attempted_at,
        )

    def test_a_loop_attempting_on_cadence_reads_withheld_not_frozen(self) -> None:
        self._seed("withholder", attempted_at=timezone.now())
        message = recon._check_enabled_loops_ticked().message
        assert "withheld" in message.lower()
        assert "`withholder`" in message

    def test_a_loop_nothing_attempts_reads_frozen(self) -> None:
        self._seed("dead", attempted_at=None)
        message = recon._check_enabled_loops_ticked().message
        assert "frozen" in message.lower()
        assert "`dead`" in message

    def test_the_two_are_named_apart_in_one_message(self) -> None:
        self._seed("withholder", attempted_at=timezone.now())
        self._seed("dead", attempted_at=None)
        message = recon._check_enabled_loops_ticked().message
        frozen, withheld = message.lower().find("frozen"), message.lower().find("withheld")
        assert frozen != -1
        assert withheld != -1
        assert frozen != withheld

    def test_a_withheld_loop_is_not_sent_after_the_worker(self) -> None:
        # The frozen remedy (start the worker) is the wrong errand for a loop whose
        # ticks are landing — it is the misdirection #4355 was filed about.
        self._seed("withholder", attempted_at=timezone.now())
        assert "t3 worker ensure" not in recon._check_enabled_loops_ticked().message

    def test_an_attempt_as_stale_as_the_run_is_frozen_not_withheld(self) -> None:
        self._seed("dead", attempted_at=timezone.now() - dt.timedelta(days=7))
        message = recon._check_enabled_loops_ticked().message
        assert "frozen" in message.lower()
        assert "withheld" not in message.lower()


class VacuousEvalGateTestCase(TestCase):
    def test_no_runs_is_ok(self) -> None:
        assert recon._check_vacuous_eval_gates().level == "ok"

    def test_run_with_zero_graded_scenarios_alarms(self) -> None:
        EvalRunRecord.objects.record(model="opus")
        finding = recon._check_vacuous_eval_gates()
        assert finding.is_alarm
        assert "Vacuous-gate" in finding.message

    def test_run_with_a_graded_scenario_is_ok(self) -> None:
        run = EvalRunRecord.objects.record(model="opus")
        run.record_scenario(scenario_name="s1", verdict=EvalVerdict.PASS)
        assert recon._check_vacuous_eval_gates().level == "ok"

    def test_run_with_only_skipped_scenarios_still_alarms(self) -> None:
        run = EvalRunRecord.objects.record(model="opus")
        run.record_scenario(scenario_name="s1", verdict=EvalVerdict.SKIP)
        assert recon._check_vacuous_eval_gates().is_alarm


class HaltCountTestCase(TestCase):
    def test_no_halts_is_ok(self) -> None:
        assert recon._check_halt_count().level == "ok"

    def test_halts_over_threshold_alarms(self) -> None:
        for i in range(recon.MAX_HALTS_PER_DAY + 1):
            DeferredQuestion.record(
                f"repair-loop stall {i}",
                dedupe_marker=f"repair-stall:{i}:coding",
                audience=DeferredQuestion.Audience.INTERNAL,
            )
        finding = recon._check_halt_count()
        assert finding.is_alarm
        assert "Repair-halt" in finding.message

    def test_non_repair_questions_not_counted(self) -> None:
        for i in range(recon.MAX_HALTS_PER_DAY + 1):
            DeferredQuestion.record(f"ordinary question {i}")
        assert recon._check_halt_count().level == "ok"


class OpenQuestionAgeTestCase(TestCase):
    def test_no_questions_is_ok(self) -> None:
        assert recon._check_open_question_age().level == "ok"

    def test_old_open_question_alarms(self) -> None:
        q = DeferredQuestion.record("please decide X")
        DeferredQuestion.objects.filter(pk=q.pk).update(created_at=timezone.now() - dt.timedelta(hours=30))
        finding = recon._check_open_question_age()
        assert finding.is_alarm
        assert "Open-question-age" in finding.message

    def test_recent_open_question_is_ok(self) -> None:
        DeferredQuestion.record("please decide X")
        assert recon._check_open_question_age().level == "ok"

    def test_answered_old_question_not_alarmed(self) -> None:
        q = DeferredQuestion.record("please decide X")
        DeferredQuestion.objects.filter(pk=q.pk).update(created_at=timezone.now() - dt.timedelta(hours=30))
        DeferredQuestion.consume(q.pk, answer="do it")
        assert recon._check_open_question_age().level == "ok"


class DuplicateExecutionTestCase(TestCase):
    def test_no_duplicates_is_ok(self) -> None:
        assert recon._check_duplicate_execution().level == "ok"

    def test_task_with_two_successes_alarms(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        TaskAttempt.objects.create(task=task, exit_code=0)
        TaskAttempt.objects.create(task=task, exit_code=0)
        finding = recon._check_duplicate_execution()
        assert finding.is_alarm
        assert "Duplicate-execution" in finding.message
        assert "`1`" in finding.message

    def test_single_success_is_ok(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        TaskAttempt.objects.create(task=task, exit_code=0)
        TaskAttempt.objects.create(task=task, exit_code=1, error="boom")
        assert recon._check_duplicate_execution().level == "ok"


class NotifyWiringTestCase(TestCase):
    def test_only_alarms_are_dmd_with_per_day_key(self) -> None:
        Ticket.objects.create()
        ticket = Ticket.objects.create()
        _attempt(ticket, cost=42.0)  # cost-per-delivered alarm: spend, zero deliveries
        moment = dt.datetime(2026, 7, 23, 12, 0, tzinfo=dt.UTC)
        with patch("teatree.core.notify.notify_user") as notify:
            findings = recon.reconcile_and_notify(moment)
        alarms = [f for f in findings if f.is_alarm]
        assert notify.call_count == len(alarms) >= 1
        keys = {call.kwargs["idempotency_key"] for call in notify.call_args_list}
        assert all(key.endswith(":2026-07-23") for key in keys)
        assert all(call.kwargs["audience"] == recon.NotifyAudience.OWNER_ESCALATION for call in notify.call_args_list)

    def test_clean_state_dms_nothing(self) -> None:
        Loop.objects.all().delete()  # the seeded default loops would otherwise alarm the freeze check
        with patch("teatree.core.notify.notify_user") as notify:
            reconcile_and_notify()
        notify.assert_not_called()

    def test_failed_dm_is_surfaced_locally_not_fatal(self) -> None:
        # The owner-DM channel is best-effort: a raising notify_user is caught and
        # echoed as a WARN, and the ledger run still returns its findings.
        ticket = Ticket.objects.create()
        _attempt(ticket, cost=42.0)  # a cost-per-delivered alarm to DM
        buf = io.StringIO()
        with (
            patch("teatree.core.notify.notify_user", side_effect=RuntimeError("slack down")),
            redirect_stdout(buf),
        ):
            findings = reconcile_and_notify()
        assert any(f.is_alarm for f in findings)
        assert "Reconciliation DM failed" in buf.getvalue()


class DoctorHookTestCase(TestCase):
    def test_hook_returns_true_and_surfaces_alarms(self) -> None:
        ticket = Ticket.objects.create()
        _attempt(ticket, cost=42.0)
        buf = io.StringIO()
        with patch("teatree.core.notify.notify_user"), redirect_stdout(buf):
            result = recon._check_reconciliation_ledger()
        out = buf.getvalue()
        assert result is True  # surfacing-only: never reddens the exit code
        assert "WARN" in out
        assert "Cost-per-delivered-ticket" in out

    def test_hook_is_clean_when_healthy(self) -> None:
        Loop.objects.all().delete()  # the seeded default loops would otherwise alarm the freeze check
        buf = io.StringIO()
        with patch("teatree.core.notify.notify_user"), redirect_stdout(buf):
            result = recon._check_reconciliation_ledger()
        assert result is True
        assert "alarm" not in buf.getvalue().lower()

    def test_run_returns_one_finding_per_check(self) -> None:
        findings = run_reconciliation_checks()
        assert len(findings) == len(recon.CHECKS)
        assert {f.check_id for f in findings} == {
            "park_rows_per_day",
            "cost_per_delivered_ticket",
            "dead_ticket_spend",
            "enabled_loops_ticked",
            "green_ci_check_ran_a_case",
            "halt_count_24h",
            "open_question_age",
            "duplicate_execution_count",
            "high_churn_table_size",
            "review_dispatch_saturation",
            "external_output_vs_internal_success",
            "merged_without_verdict",
        }


class DegradedReadTestCase(TestCase):
    def test_crashed_read_degrades_not_alarms(self) -> None:
        # The window read runs inside each check's try/except; a raising clock
        # models any read-path crash (DB down, unmigrated self-DB) degrading to
        # a non-alarm rather than reddening the run or firing a false alarm.
        with patch.object(recon, "_now", side_effect=RuntimeError("db down")):
            finding = recon._check_duplicate_execution()
        assert finding.level == "degraded"
        assert not finding.is_alarm

    def test_ledger_survives_a_degraded_check(self) -> None:
        with patch.object(recon, "_check_duplicate_execution", side_effect=RuntimeError("boom")):
            # A check that raises OUTSIDE its own guard would still not crash the
            # doctor hook — the hook wraps the whole run.
            buf = io.StringIO()
            with patch("teatree.core.notify.notify_user"), redirect_stdout(buf):
                result = recon._check_reconciliation_ledger()
        assert result is True

    def test_hook_survives_a_crashing_ledger_run(self) -> None:
        # If the whole ledger run raises (not just one guarded check), the doctor
        # hook catches it, echoes a WARN, and still returns True — never reddens
        # the exit code the watchdog keys on.
        buf = io.StringIO()
        with (
            patch.object(recon, "reconcile_and_notify", side_effect=RuntimeError("ledger boom")),
            redirect_stdout(buf),
        ):
            result = recon._check_reconciliation_ledger()
        assert result is True
        assert "Reconciliation ledger crashed" in buf.getvalue()


class HighChurnTableSizeTestCase(TestCase):
    def test_under_ceiling_is_ok(self) -> None:
        ticket = Ticket.objects.create()
        _attempt(ticket)
        finding = recon._check_high_churn_table_size()
        assert finding.level == "ok"
        assert "TaskAttempt" in finding.message

    def test_task_attempt_over_ceiling_alarms(self) -> None:
        ticket = Ticket.objects.create()
        _attempt(ticket)
        _attempt(ticket)
        with patch.object(recon, "MAX_TASK_ATTEMPT_ROWS", 1):
            finding = recon._check_high_churn_table_size()
        assert finding.is_alarm
        assert "TaskAttempt" in finding.message
        assert "retention prune" in finding.message

    def test_incoming_event_over_ceiling_alarms(self) -> None:
        IncomingEvent.objects.create(source=IncomingEvent.Source.SLACK, idempotency_key="e1")
        IncomingEvent.objects.create(source=IncomingEvent.Source.SLACK, idempotency_key="e2")
        with patch.object(recon, "MAX_INCOMING_EVENT_ROWS", 1):
            finding = recon._check_high_churn_table_size()
        assert finding.is_alarm
        assert "IncomingEvent" in finding.message

    def test_at_ceiling_is_ok(self) -> None:
        ticket = Ticket.objects.create()
        _attempt(ticket)
        with patch.object(recon, "MAX_TASK_ATTEMPT_ROWS", 1):
            finding = recon._check_high_churn_table_size()
        assert finding.level == "ok"

    def test_ticket_transition_over_ceiling_alarms(self) -> None:
        """The 3.2M-row table the check was blind to until it had a lane (#3871)."""
        ticket = Ticket.objects.create()
        for _ in range(2):
            TicketTransition(ticket=ticket, from_state="started", to_state="coded").save()
        with patch.object(recon, "MAX_TICKET_TRANSITION_ROWS", 1):
            finding = recon._check_high_churn_table_size()
        assert finding.is_alarm
        assert "TicketTransition" in finding.message

    def test_task_result_over_ceiling_alarms(self) -> None:
        for _ in range(2):
            DBTaskResult.objects.create(
                args_kwargs={"args": [], "kwargs": {}},
                task_path="x.y",
                backend_name="default",
                run_after=timezone.now(),
                exception_class_path="",
                traceback="",
            )
        with patch.object(recon, "MAX_TASK_RESULT_ROWS", 1):
            finding = recon._check_high_churn_table_size()
        assert finding.is_alarm
        assert "DBTaskResult" in finding.message


class TestReviewDispatchSaturation(TestCase):
    """The bounded retry must be visible when it runs out, or it is a quieter deadlock."""

    SLUG = "souliane/teatree"
    HEAD = "b" * 40

    def _claim(self, *, attempts: int, expired: bool) -> AutoReviewDispatch:
        row = AutoReviewDispatch.enqueue(
            slug=self.SLUG, pr_id=3920, head_sha=self.HEAD, pr_url=f"https://github.com/{self.SLUG}/pull/3920"
        )
        assert row is not None
        deadline = timezone.now() - dt.timedelta(minutes=1) if expired else timezone.now() + dt.timedelta(hours=1)
        AutoReviewDispatch.objects.filter(pk=row.pk).update(attempts=attempts, deadline=deadline)
        row.refresh_from_db()
        return row

    def test_a_healthy_factory_is_ok(self) -> None:
        self._claim(attempts=1, expired=False)
        finding = recon._check_review_dispatch_saturation()
        assert not finding.is_alarm

    def test_an_expired_claim_with_budget_left_is_not_saturation(self) -> None:
        # It will simply be re-armed by the next sweep — alarming here would make
        # every ordinary crashed reviewer page the owner.
        self._claim(attempts=1, expired=True)
        assert not recon._check_review_dispatch_saturation().is_alarm

    def test_an_exhausted_expired_claim_alarms_and_names_the_pr(self) -> None:
        self._claim(attempts=MAX_DISPATCH_ATTEMPTS, expired=True)

        finding = recon._check_review_dispatch_saturation()

        assert finding.is_alarm
        assert f"{self.SLUG}#3920" in finding.message

    def test_a_resolved_claim_never_alarms(self) -> None:
        self._claim(attempts=MAX_DISPATCH_ATTEMPTS, expired=True)
        AutoReviewDispatch.mark_resolved(slug=self.SLUG, pr_id=3920, head_sha=self.HEAD)
        assert not recon._check_review_dispatch_saturation().is_alarm

    def test_a_saturated_codex_claim_alarms_and_is_labelled(self) -> None:
        row = CodexReviewMarker.claim(slug=self.SLUG, pr_id=1254, head_sha=self.HEAD)
        assert row is not None
        CodexReviewMarker.objects.filter(pk=row.pk).update(
            attempts=MAX_DISPATCH_ATTEMPTS, deadline=timezone.now() - dt.timedelta(minutes=1)
        )

        finding = recon._check_review_dispatch_saturation()

        assert finding.is_alarm
        assert f"codex:{self.SLUG}#1254" in finding.message

    def test_the_check_is_registered_in_the_ledger(self) -> None:
        assert recon._check_review_dispatch_saturation in recon.CHECKS


class AdmittedLoopFreezeTestCase(TestCase):
    """The 24h freeze alarm covers whatever the verdict admits, not the raw column (#4185).

    A preset-forced-on loop froze invisibly — this bug's exact signature — while a
    preset-masked-off enabled loop false-alarmed for standing still as instructed.
    """

    def setUp(self) -> None:
        Loop.objects.all().delete()

    @staticmethod
    def _activate(entries: dict[str, bool]) -> None:
        Mode.objects.create(name="preset-4185", entries=entries)
        ModeOverride.objects.set_override("preset-4185")

    def test_a_preset_forced_on_column_disabled_loop_that_never_ticked_alarms(self) -> None:
        Loop.objects.create(name="probe", script="src/teatree/loops/probe/loop.py", delay_seconds=60, enabled=False)
        self._activate({"probe": True})
        finding = recon._check_enabled_loops_ticked()
        assert finding.is_alarm
        assert "`probe`" in finding.message

    def test_a_preset_masked_off_column_enabled_loop_does_not_alarm(self) -> None:
        Loop.objects.create(name="probe", script="src/teatree/loops/probe/loop.py", delay_seconds=60, enabled=True)
        self._activate({"probe": False})
        assert recon._check_enabled_loops_ticked().level == "ok"

    def test_a_held_loop_does_not_alarm(self) -> None:
        Loop.objects.create(name="probe", script="src/teatree/loops/probe/loop.py", delay_seconds=60, enabled=True)
        LoopState.objects.pause("probe")
        assert recon._check_enabled_loops_ticked().level == "ok"


class _RaisingForgeHost:
    def list_merged_prs_since(self, *, repo: str, since: str) -> list[dict[str, object]]:
        message = f"HTTP 403 for {repo} since {since}"
        raise RuntimeError(message)


def _seed_snapshot(
    *,
    merged: int = 0,
    refs: list[dict[str, object]] | None = None,
    status: str = "ok",
    at: dt.datetime | None = None,
) -> ExternalOutcomeSnapshot:
    """A snapshot inside the TTL, so the checks read it instead of the network."""
    return ExternalOutcomeSnapshot.objects.create(
        generated_at=at or timezone.now(),
        window_days=DEFAULT_EXTERNAL_WINDOW_DAYS,
        status=status,
        repo_slugs=["acme/app"],
        merged_pr_count=merged if refs is None else len(refs),
        merged_pr_refs=refs or [],
    )


def _successes(count: int) -> None:
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    for _ in range(count):
        task = Task.objects.create(ticket=ticket, session=session)
        TaskAttempt.objects.create(task=task, exit_code=0)


class ExternalOutputVsInternalSuccessTestCase(TestCase):
    """The one measure internal bookkeeping cannot satisfy: what the forge says landed."""

    def test_sustained_internal_success_with_zero_forge_merges_alarms_naming_both(self) -> None:
        _successes(external.MIN_INTERNAL_SUCCESSES_FOR_OUTCOME + 5)
        _seed_snapshot(merged=0)

        finding = external._check_external_output_vs_internal_success()

        assert finding.is_alarm
        assert f"`{external.MIN_INTERNAL_SUCCESSES_FOR_OUTCOME + 5}`" in finding.message
        assert "`0` pull requests merged" in finding.message

    def test_forge_merges_in_the_window_is_ok(self) -> None:
        _successes(external.MIN_INTERNAL_SUCCESSES_FOR_OUTCOME + 5)
        _seed_snapshot(merged=4)

        assert external._check_external_output_vs_internal_success().level == "ok"

    def test_idle_window_is_ok_not_an_alarm(self) -> None:
        _seed_snapshot(merged=0)

        finding = external._check_external_output_vs_internal_success()

        assert finding.level == "ok"
        assert not finding.is_alarm

    def test_no_forge_is_degraded_never_ok_and_never_the_alarm(self) -> None:
        _successes(external.MIN_INTERNAL_SUCCESSES_FOR_OUTCOME + 5)
        _seed_snapshot(merged=0, status="no_forge")

        finding = external._check_external_output_vs_internal_success()

        assert finding.level == "degraded"
        assert not finding.is_alarm
        assert "could not measure external output" in finding.message

    def test_failed_forge_read_is_degraded_never_a_fabricated_zero(self) -> None:
        _successes(external.MIN_INTERNAL_SUCCESSES_FOR_OUTCOME + 5)
        forge = Forge(host=_RaisingForgeHost(), repo_slugs=("acme/app",))

        with patch("teatree.core.factory.external_outcomes.resolve_forge", return_value=forge):
            finding = external._check_external_output_vs_internal_success()

        assert finding.level == "degraded"
        assert not finding.is_alarm
        assert ExternalOutcomeSnapshot.objects.count() == 0


class MergedWithoutVerdictTestCase(TestCase):
    """The denominator is forge-side, so internal success alone can never satisfy it."""

    @staticmethod
    def _refs(count: int) -> list[dict[str, object]]:
        return [{"slug": "acme/app", "number": n, "url": ""} for n in range(1, count + 1)]

    @staticmethod
    def _verdict(number: int) -> None:
        ReviewVerdict.objects.create(
            pr_id=number,
            slug="acme/app",
            reviewed_sha="a" * 40,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="cold-reviewer",
            blast_class=MergeClear.BlastClass.LOGIC,
            gh_verify_result=MergeClear.VerifyResult.GREEN,
        )

    def test_merges_without_verdicts_alarm_naming_both_numbers(self) -> None:
        _seed_snapshot(refs=self._refs(5))
        self._verdict(1)

        finding = external._check_merged_without_verdict()

        assert finding.is_alarm
        assert "`5` pull request(s) merged" in finding.message
        assert "only `1` carry a recorded ReviewVerdict" in finding.message
        assert "`acme/app#2`" in finding.message

    def test_below_the_floor_is_ok(self) -> None:
        _seed_snapshot(refs=self._refs(5))
        for number in range(1, 5):
            self._verdict(number)

        finding = external._check_merged_without_verdict()

        assert finding.level == "ok"
        assert "4/5" in finding.message

    def test_every_merge_vouched_is_ok(self) -> None:
        _seed_snapshot(refs=self._refs(3))
        for number in range(1, 4):
            self._verdict(number)

        assert external._check_merged_without_verdict().level == "ok"

    def test_no_forge_is_degraded_never_ok(self) -> None:
        _seed_snapshot(merged=0, status="no_forge")

        assert external._check_merged_without_verdict().level == "degraded"

    def test_a_crashing_read_is_degraded_never_a_crash(self) -> None:
        with patch("teatree.core.models.ReviewVerdict.objects.for_pr", side_effect=RuntimeError("boom")):
            _seed_snapshot(refs=self._refs(1))
            finding = external._check_merged_without_verdict()

        assert finding.level == "degraded"
        assert not finding.is_alarm


class ExternalChecksAreRegisteredTestCase(TestCase):
    def test_both_external_checks_run_in_the_ledger(self) -> None:
        _seed_snapshot(merged=1)

        ids = {finding.check_id for finding in run_reconciliation_checks()}

        assert "external_output_vs_internal_success" in ids
        assert "merged_without_verdict" in ids

    def test_the_two_checks_share_one_forge_read_per_cadence(self) -> None:
        host = _CountingForgeHost()
        forge = Forge(host=host, repo_slugs=("acme/app",))

        with patch("teatree.core.factory.external_outcomes.resolve_forge", return_value=forge):
            run_reconciliation_checks()

        assert len(host.calls) == 1
        assert ExternalOutcomeSnapshot.objects.count() == 1


class _CountingForgeHost:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_merged_prs_since(self, *, repo: str, since: str) -> list[dict[str, object]]:
        self.calls.append(repo)
        return [{"number": 1, "html_url": "https://example.test/pr/1"}]


class PersistedRefParsingTestCase(TestCase):
    """A malformed persisted ref is dropped, never coerced into a bogus `slug#0` merge."""

    def test_malformed_entries_are_dropped_not_guessed(self) -> None:
        refs = [
            {"slug": "acme/app", "number": 1},
            {"slug": "acme/app", "number": "not-a-number"},
            {"slug": None, "number": 2},
            "not-a-mapping",
        ]

        assert external._pr_ref_pairs(refs) == [("acme/app", 1)]

    def test_a_non_list_payload_yields_no_refs(self) -> None:
        assert external._pr_ref_pairs({"slug": "acme/app"}) == []

    def test_a_malformed_ref_is_not_counted_as_an_unvouched_merge(self) -> None:
        _seed_snapshot(refs=[{"slug": "acme/app", "number": "?"}] * 4)

        finding = external._check_merged_without_verdict()

        assert finding.level == "ok"
        assert not finding.is_alarm
