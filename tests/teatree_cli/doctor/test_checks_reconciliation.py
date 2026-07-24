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

from teatree.cli.doctor import checks_reconciliation as recon
from teatree.cli.doctor.checks_reconciliation import reconcile_and_notify, run_reconciliation_checks
from teatree.core.models import DeferredQuestion, EvalRunRecord, Loop, Session, Task, TaskAttempt, Ticket
from teatree.core.models.eval_run import EvalVerdict
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX


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
            "enabled_loops_ticked_24h",
            "green_ci_check_ran_a_case",
            "halt_count_24h",
            "open_question_age",
            "duplicate_execution_count",
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
