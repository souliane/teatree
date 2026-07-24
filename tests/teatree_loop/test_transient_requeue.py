"""Bounded auto-requeue of transient-FAILED tasks — the retry, hard-bounded.

A task that RETURNS a transient failure (an outage envelope, a provisioning-step
failure, an incomplete run, a coder yield that landed no commit) lands terminal
FAILED and, before this sweep, stayed there forever with no retry. The sweep
reopens it (FAILED → PENDING) so the loop resumes — but ONLY within the #2009
repair-loop budget: a phase at its iteration cap, or stalled on two identical
failures, is NOT reopened and is escalated LOUDLY via a durable
``DeferredQuestion``. A DETERMINISTIC failure (a test failure, an assertion) is
never reopened. The hardest pin: it NEVER retries endlessly.
"""

from datetime import datetime, timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.repair_loop import max_phase_iterations
from teatree.core.worktree.recovery_sweeps import run_boot_sweeps
from teatree.llm.anthropic_limits import LimitCause, LimitMatch
from teatree.loop.tick_recovery import _reap_stale_task_claims
from teatree.loop.transient_requeue import requeue_transient_failed


def _failed_task(*, phase: str = "coding", state: str = Ticket.State.STARTED) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.FAILED)


def _add_failed_attempt(task: Task, *, error: str, ended_at: datetime | None = None) -> None:
    TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=ended_at or timezone.now(),
        exit_code=1,
        error=error,
    )
    Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED)


def _exhaustion_error(cause: LimitCause) -> str:
    """The real ``error`` string a limit-killed attempt records (``LimitMatch.as_reason``)."""
    return LimitMatch(phrase="5-hour limit", cause=cause).as_reason()


class TestTransientRequeue(TestCase):
    def test_transient_failed_task_is_reopened_once(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error="outage_death: connection refused")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 1
        assert task.status == Task.Status.PENDING
        # A second pass finds it PENDING (no longer FAILED) — no double reopen.
        assert requeue_transient_failed() == 0

    def test_poison_row_does_not_abort_the_sweep(self) -> None:
        # #3441: one FAILED task whose processing raises an unexpected exception must NOT
        # abort the whole sweep and strand every OTHER loop's recoverable task. The poison
        # row is skipped (left FAILED, logged), the healthy row still recovers.
        poison = _failed_task()  # created first ⇒ lower pk ⇒ processed first
        _add_failed_attempt(poison, error="outage_death: poison row")
        healthy = _failed_task()
        _add_failed_attempt(healthy, error="outage_death: connection refused")

        def _raise_on_poison(error: str) -> bool:
            if "poison" in error:
                msg = "classifier blew up on the poison row"
                raise ValueError(msg)
            return True

        with mock.patch("teatree.loop.transient_requeue.is_transient_failure", side_effect=_raise_on_poison):
            reopened = requeue_transient_failed()

        healthy.refresh_from_db()
        poison.refresh_from_db()
        assert reopened == 1
        assert healthy.status == Task.Status.PENDING  # the sweep kept going past the poison row
        assert poison.status == Task.Status.FAILED  # the poison row is skipped, never fatal

    def test_empty_error_failed_task_is_escalated_not_frozen(self) -> None:
        # A FAILED task with NO recorded error matches neither the transient nor the
        # deterministic branch; without a route it froze silently — it must escalate.
        task = _failed_task()  # no attempt → empty latest error

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_answered_escalation_does_not_re_escalate(self) -> None:
        # #6: once escalated and parked, answering the question must NOT spawn a fresh
        # escalation on the next tick — the row stamp is the durable, once-per-task dedup.
        task = _failed_task()
        _add_failed_attempt(task, error="AssertionError: expected 3 got 4")
        assert requeue_transient_failed() == 0
        question = DeferredQuestion.objects.get()
        question.answered_at = timezone.now()
        question.save(update_fields=["answered_at"])

        assert requeue_transient_failed() == 0  # the parked row is excluded from the scan
        assert DeferredQuestion.objects.count() == 1  # no second question after the answer

    def test_deterministic_failed_task_is_not_reopened(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error="AssertionError: expected 3 got 4")

        assert requeue_transient_failed() == 0

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_failed_task_on_terminal_ticket_is_not_reopened(self) -> None:
        task = _failed_task(state=Ticket.State.SHIPPED)
        _add_failed_attempt(task, error="outage_death: connection refused")

        assert requeue_transient_failed() == 0

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_identical_double_failure_is_escalated_not_reopened(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error="result_error: no terminal ResultMessage")
        _add_failed_attempt(task, error="result_error: no terminal ResultMessage")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_budget_exhausted_is_escalated_not_reopened(self) -> None:
        cap = max_phase_iterations()
        task = _failed_task()
        # DISTINCT transient errors so no stall — the CAP is what stops it.
        for i in range(cap):
            _add_failed_attempt(task, error=f"result_error: attempt {'x' * (i + 1)} died")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_tick_recovery_reopens_transient_failed(self) -> None:
        # The sweep is wired into the loop tick's recovery step, not just callable.
        task = _failed_task()
        _add_failed_attempt(task, error="outage_death: connection refused")

        _reap_stale_task_claims()

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_deterministic_evidence_refusal_gets_one_corrective_retry(self) -> None:
        # A coding task that landed FAILED on a missing files_modified envelope,
        # with NO committed work to salvage, gets ONE bounded corrective retry:
        # reopened PENDING with the envelope-emit instruction appended to the
        # prompt (execution_reason). A terminal FAILED on a non-terminal ticket
        # must never sit silent.
        task = _failed_task()
        _add_failed_attempt(
            task,
            error="missing required evidence for phase 'coding': result must include one of [files_modified]",
        )

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 1
        assert task.status == Task.Status.PENDING
        assert "files_modified" in task.execution_reason
        assert "envelope" in task.execution_reason.lower()

    def test_deterministic_refusal_after_corrective_retry_is_escalated(self) -> None:
        task = _failed_task()
        _add_failed_attempt(
            task,
            error="missing required evidence for phase 'coding': result must include one of [files_modified]",
        )
        # First sweep: the corrective retry reopens it.
        assert requeue_transient_failed() == 1
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

        # It fails AGAIN with a different deterministic error (no stall) — the
        # corrective retry was already spent, so it must escalate, not retry.
        _add_failed_attempt(task, error="AssertionError: still no envelope emitted")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_non_envelope_deterministic_failure_is_escalated_not_retried(self) -> None:
        # A real test failure is not an omitted-envelope class: no corrective
        # retry (the envelope note would be misleading) — escalate so it never
        # sits silent.
        task = _failed_task()
        _add_failed_attempt(task, error="AssertionError: expected 3 got 4")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_deterministic_non_coding_failure_is_escalated_not_retried(self) -> None:
        # A planning evidence refusal is not a coder-envelope class: the coder
        # note would be wrong, so no corrective retry — escalate instead.
        task = _failed_task(phase="planning")
        _add_failed_attempt(task, error="missing required evidence for phase 'planning'")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert "[auto-corrective-retry]" not in task.execution_reason
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_deterministic_failure_over_budget_is_escalated(self) -> None:
        cap = max_phase_iterations()
        task = _failed_task()
        for i in range(cap):
            _add_failed_attempt(task, error=f"AssertionError variant {'x' * (i + 1)}")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_deterministic_failure_on_terminal_ticket_is_left_alone(self) -> None:
        task = _failed_task(state=Ticket.State.SHIPPED)
        _add_failed_attempt(task, error="AssertionError: expected 3 got 4")

        assert requeue_transient_failed() == 0

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_superseded_failed_task_is_retired_not_escalated(self) -> None:
        # 3366/3336/3352: a ticket whose FSM already reached a phase's output
        # (state TESTED ⇒ testing done) can still carry a stale FAILED testing task
        # from an earlier interrupted run. It must be retired silently — never
        # escalated as an away-mode question the ticket's own state already answers.
        task = _failed_task(phase="testing", state=Ticket.State.TESTED)
        _add_failed_attempt(task, error="result_error: no terminal ResultMessage")
        _add_failed_attempt(task, error="result_error: no terminal ResultMessage")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.COMPLETED  # retired, not left FAILED
        assert "[superseded-retired]" in task.execution_reason
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_live_phase_not_yet_reached_still_escalates(self) -> None:
        # Boundary guard: a FAILED task for a phase the ticket has NOT reached
        # (state TESTED, phase reviewing ⇒ produces REVIEWED, not yet reached) is a
        # genuinely blocked phase — it must still escalate, never be silently retired.
        task = _failed_task(phase="reviewing", state=Ticket.State.TESTED)
        _add_failed_attempt(task, error="missing required evidence for phase 'reviewing'")
        _add_failed_attempt(task, error="missing required evidence for phase 'reviewing'")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_lease_loss_with_live_successor_is_parked_not_escalated(self) -> None:
        # 3534: worker A loses its lease because a redispatch minted a fresh task B
        # for the same (ticket, phase) and B re-claimed the lease. A lands FAILED
        # carrying the `stuck_loop: lease lost … re-claimed` breach even though the
        # phase is recovering fine under B. The predecessor must park silently —
        # escalating it asks the human about a failure the system already superseded.
        # It stays FAILED (the phase never finished) and drops out of every later scan.
        predecessor = _failed_task(phase="coding")
        _add_failed_attempt(
            predecessor,
            error="stuck_loop: lease lost for task 1: re-claimed by another worker",
        )
        Task.objects.create(
            ticket=predecessor.ticket,
            session=predecessor.session,
            phase="coding",
            status=Task.Status.CLAIMED,
        )

        reopened = requeue_transient_failed()

        predecessor.refresh_from_db()
        assert reopened == 0
        assert predecessor.status == Task.Status.FAILED
        assert "[superseded-parked]" in predecessor.execution_reason
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0
        # The park is durable: a later sweep never resurrects it into an escalation.
        assert requeue_transient_failed() == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_failed_task_with_only_an_older_sibling_still_escalates(self) -> None:
        # Directionality guard: the newest FAILED row must NOT be retired on the
        # strength of an OLDER live sibling — only a LATER successor (higher pk)
        # supersedes it. A genuinely blocked phase whose live sibling predates it
        # is still a real halt that must escalate.
        older = _failed_task(phase="coding")
        newest = Task.objects.create(
            ticket=older.ticket,
            session=older.session,
            phase="coding",
            status=Task.Status.FAILED,
        )
        Task.objects.filter(pk=older.pk).update(status=Task.Status.CLAIMED)
        _add_failed_attempt(newest, error="deterministic failure in phase 'coding'")

        reopened = requeue_transient_failed()

        newest.refresh_from_db()
        assert reopened == 0
        assert newest.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_failed_task_with_terminal_sibling_still_escalates(self) -> None:
        # A COMPLETED/FAILED sibling is not a live successor — the phase is not being
        # worked by anyone else, so a genuine deterministic failure must still escalate.
        task = _failed_task(phase="coding")
        _add_failed_attempt(task, error="deterministic failure in phase 'coding'")
        Task.objects.create(
            ticket=task.ticket,
            session=task.session,
            phase="coding",
            status=Task.Status.COMPLETED,
        )

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_live_successor_park_leaves_the_ticket_fsm_untouched(self) -> None:
        # The park must not advance the ticket past a phase that never completed. A row
        # marked COMPLETED becomes the newest completed task for its ticket, so the
        # boot-sweep replay fires its phase transition and a PLANNED ticket silently
        # reaches CODED while the successor is still mid-flight.
        predecessor = _failed_task(phase="coding", state=Ticket.State.PLANNED)
        _add_failed_attempt(
            predecessor,
            error="stuck_loop: lease lost for task 1: re-claimed by another worker",
        )
        Task.objects.create(
            ticket=predecessor.ticket,
            session=predecessor.session,
            phase="coding",
            status=Task.Status.CLAIMED,
        )

        assert requeue_transient_failed() == 0
        counts = run_boot_sweeps()

        predecessor.ticket.refresh_from_db()
        assert counts.replayed_transitions == 0
        assert predecessor.ticket.state == Ticket.State.PLANNED
        assert not predecessor.ticket.tasks.completed_in_phase("coding").exists()

    def test_live_successor_park_does_not_satisfy_the_review_completion_guard(self) -> None:
        # Same skip on the review seam: a COMPLETED park row satisfies
        # ``completed_in_phase("reviewing")`` — the guard on Ticket.review() /
        # mark_reviewed_externally() — so the replay disposes a TESTED ticket as if a
        # verdict had landed.
        predecessor = _failed_task(phase="reviewing", state=Ticket.State.TESTED)
        _add_failed_attempt(
            predecessor,
            error="stuck_loop: lease lost for task 1: re-claimed by another worker",
        )
        Task.objects.create(
            ticket=predecessor.ticket,
            session=predecessor.session,
            phase="reviewing",
            status=Task.Status.CLAIMED,
        )

        assert requeue_transient_failed() == 0
        counts = run_boot_sweeps()

        predecessor.ticket.refresh_from_db()
        assert counts.replayed_transitions == 0
        assert predecessor.ticket.state == Ticket.State.TESTED
        assert not predecessor.ticket.tasks.completed_in_phase("reviewing").exists()

    def test_churned_tasks_same_condition_collapse_to_one_question(self) -> None:
        # THE FLOOD FIX: a stuck phase mints a FRESH Task row every redispatch cycle.
        # Two FAILED tasks on the same (ticket, phase) failing IDENTICALLY are ONE
        # standing condition — they must collapse to a single open DeferredQuestion,
        # not one per task (the observed 10-15x duplicate flood).
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.TESTED)
        for _ in range(2):
            session = Session.objects.create(ticket=ticket, agent_id="review")
            task = Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.FAILED)
            _add_failed_attempt(task, error="missing required evidence for phase 'reviewing'")

        requeue_transient_failed()

        # One condition ⇒ one question, despite two distinct FAILED task rows.
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_distinct_conditions_do_not_over_collapse(self) -> None:
        # Guard on the dedup: two DIFFERENT failures on the same (ticket, phase) are
        # two conditions — they must NOT collapse into one, or a real second problem
        # would be hidden behind the first.
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.TESTED)
        errors = ("missing required evidence for phase 'reviewing'", "AssertionError: reviewer crashed")
        for error in errors:
            session = Session.objects.create(ticket=ticket, agent_id="review")
            task = Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.FAILED)
            _add_failed_attempt(task, error=error)

        requeue_transient_failed()

        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 2

    def test_bounded_never_retries_endlessly(self) -> None:
        cap = max_phase_iterations()
        task = _failed_task()
        distinct_errors = [
            "outage_death: alpha",
            "result_error: beta gone",
            "provision_failed: gamma missing",
            "landing_unverified: delta uncommitted",
            "outage_death: epsilon",
            "result_error: zeta gone",
            "provision_failed: eta missing",
        ]
        reopens = 0
        for error in distinct_errors:
            _add_failed_attempt(task, error=error)
            reopens += requeue_transient_failed()
            task.refresh_from_db()

        # Reopens are bounded by the per-phase cap — never once per tick forever.
        assert reopens == cap - 1
        assert task.status == Task.Status.FAILED

        # Escalated exactly once; re-running the sweep many more times never
        # reopens again and never spams another escalation.
        questions_after_exhaustion = DeferredQuestion.objects.filter(answered_at__isnull=True).count()
        assert questions_after_exhaustion == 1
        for _ in range(10):
            assert requeue_transient_failed() == 0
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1


class TestExhaustionAutoRequeue(TestCase):
    """#3407: exhaustion-killed FAILED tasks auto-requeue once their window resets.

    A task that died on a subscription session/weekly or transient rate limit is a
    capacity failure, not a defect — while ``limit_autorecovery_enabled`` is ON it is
    reopened once the window HORIZON has elapsed, never escalated to a human. API-credit
    exhaustion (no timed reset) and the flag-off path keep the existing escalation.
    """

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=True)

    def test_session_limit_task_is_reopened_after_the_window_resets(self) -> None:
        task = _failed_task()
        # The 5h session window has elapsed since the failure → capacity is back.
        _add_failed_attempt(
            task, error=_exhaustion_error(LimitCause.SUBSCRIPTION_SESSION), ended_at=timezone.now() - timedelta(hours=6)
        )

        assert requeue_transient_failed() == 1
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        # No human question — a capacity dip is auto-recovered, not escalated.
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_session_limit_task_with_no_ended_at_is_reopened_not_stranded(self) -> None:
        # #3444: a limit-killed attempt can land FAILED having NEVER recorded ended_at
        # (a crash/kill after the failure classification, before the row was finalized).
        # The old ``ended is None`` guard stranded such a task forever — never past the
        # horizon, never reopened, never escalated. The horizon must anchor on started_at
        # so the task still requeues once the window has elapsed.
        task = _failed_task()
        _add_failed_attempt(task, error=_exhaustion_error(LimitCause.SUBSCRIPTION_SESSION))
        # The attempt started 6h ago (past the 5h window) and never recorded an end.
        TaskAttempt.objects.filter(task=task).update(started_at=timezone.now() - timedelta(hours=6), ended_at=None)

        assert requeue_transient_failed() == 1
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        # A capacity dip auto-recovers — no human question.
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_no_ended_at_task_is_left_failed_before_the_window_resets(self) -> None:
        # The started_at fallback must still RESPECT the horizon: an attempt that started
        # only 1h ago (no ended_at) has not cleared the 5h window and must be left FAILED
        # for a later tick, not reopened prematurely.
        task = _failed_task()
        _add_failed_attempt(task, error=_exhaustion_error(LimitCause.SUBSCRIPTION_SESSION))
        TaskAttempt.objects.filter(task=task).update(started_at=timezone.now() - timedelta(hours=1), ended_at=None)

        assert requeue_transient_failed() == 0
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_session_limit_task_is_left_failed_before_the_window_resets(self) -> None:
        task = _failed_task()
        # Only 1h since the failure — the 5h window has not reset yet.
        _add_failed_attempt(
            task, error=_exhaustion_error(LimitCause.SUBSCRIPTION_SESSION), ended_at=timezone.now() - timedelta(hours=1)
        )

        assert requeue_transient_failed() == 0
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        # Left FAILED for a later tick — NOT escalated (it is a timed wait, not a defect).
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_api_credit_task_is_escalated_not_auto_requeued(self) -> None:
        # A $0 balance has no timed reset → it must not be auto-requeued; it stays on the
        # deterministic path and is escalated so the operator adds credits.
        task = _failed_task()
        _add_failed_attempt(
            task, error=_exhaustion_error(LimitCause.API_CREDIT), ended_at=timezone.now() - timedelta(days=30)
        )

        assert requeue_transient_failed() == 0
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_repeated_session_limit_is_reopened_not_escalated_as_a_stall(self) -> None:
        # Two identical session-limit FAILED attempts in a row record two identical
        # error_fingerprints. The stall detector must NOT count usage-limit failures:
        # a capacity dip repeated across a window is auto-recovered once the horizon
        # elapses, never dead-lettered + paged to a human.
        task = _failed_task()
        err = _exhaustion_error(LimitCause.SUBSCRIPTION_SESSION)
        _add_failed_attempt(task, error=err, ended_at=timezone.now() - timedelta(hours=6))
        _add_failed_attempt(task, error=err, ended_at=timezone.now() - timedelta(hours=6))

        assert requeue_transient_failed() == 1
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_repeated_rate_limit_is_reopened_not_escalated_as_a_stall(self) -> None:
        # Same invariant for the transient rate-limit cause (a distinct recoverable
        # window): repeated identical rate-limit failures must not trip the stall.
        task = _failed_task()
        err = LimitMatch(phrase="rate limit", cause=LimitCause.RATE_LIMIT).as_reason()
        _add_failed_attempt(task, error=err, ended_at=timezone.now() - timedelta(hours=1))
        _add_failed_attempt(task, error=err, ended_at=timezone.now() - timedelta(hours=1))

        assert requeue_transient_failed() == 1
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_flag_off_keeps_the_pre_3407_escalation(self) -> None:
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=False)
        task = _failed_task()
        _add_failed_attempt(
            task, error=_exhaustion_error(LimitCause.SUBSCRIPTION_SESSION), ended_at=timezone.now() - timedelta(hours=6)
        )

        assert requeue_transient_failed() == 0
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        # Byte-identical to before #3407: an exhaustion failure follows the deterministic
        # escalation path while the flag is off.
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1


class TestDeadReviewTargetRetired(TestCase):
    """A review/codex-review task whose linked PR is CLOSED/MERGED is retired, not re-dispatched (#3556)."""

    @staticmethod
    def _reviewer_task(*, phase: str = "codex_reviewing") -> Task:
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.NOT_STARTED,
            issue_url="https://github.com/souliane/teatree/pull/3542",
        )
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.FAILED)

    def test_closed_pr_review_task_is_retired_not_reopened(self) -> None:
        task = self._reviewer_task()
        # A transient-classified error would otherwise reopen the task every tick.
        _add_failed_attempt(task, error="outage_death: agent stopped after confirming PR closed")

        with mock.patch("teatree.backends.loader.pr_is_merged_or_closed", return_value=True):
            reopened = requeue_transient_failed()

        task.refresh_from_db()
        task.ticket.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.COMPLETED  # retired, not re-queued
        assert task.ticket.state == Ticket.State.IGNORED  # terminal — drops from active scans
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_adversarial_review_phase_is_also_retired(self) -> None:
        task = self._reviewer_task(phase="codex_adversarial_reviewing")
        _add_failed_attempt(task, error="outage_death: agent stopped after confirming PR closed")

        with mock.patch("teatree.backends.loader.pr_is_merged_or_closed", return_value=True):
            reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.COMPLETED

    def test_live_pr_review_task_still_reopens(self) -> None:
        # Control: the retire path is gated on a provably-dead PR. A live/UNKNOWN PR
        # (fail-open False) must still reopen the transient failure exactly as before.
        task = self._reviewer_task()
        _add_failed_attempt(task, error="outage_death: connection refused")

        with mock.patch("teatree.backends.loader.pr_is_merged_or_closed", return_value=False):
            reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 1
        assert task.status == Task.Status.PENDING

    def test_non_review_phase_is_not_pr_gated(self) -> None:
        # Control: a non-review phase never consults PR state — a dead PR must not
        # short-circuit an ordinary coding retry.
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.FAILED)
        _add_failed_attempt(task, error="outage_death: connection refused")

        with mock.patch("teatree.backends.loader.pr_is_merged_or_closed", return_value=True) as dead:
            reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 1
        assert task.status == Task.Status.PENDING
        dead.assert_not_called()


class TestSelfRepairInsteadOfPaging(TestCase):
    """A config breach with exactly one valid resolution corrects itself and never DMs (#3665)."""

    invalid_pair = (
        "agent_harness_provider='openai_compatible' is not valid under agent_harness='claude_sdk'; "
        "valid: api_key, subscription_oauth"
    )

    def test_invalid_harness_provider_pair_is_corrected_and_the_task_reopened(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error=self.invalid_pair)

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 1
        assert task.status == Task.Status.PENDING
        assert ConfigSetting.objects.get_effective("agent_harness") == "pydantic_ai"

    def test_self_repair_never_raises_a_question_to_a_human(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error=self.invalid_pair)

        requeue_transient_failed()

        assert DeferredQuestion.objects.count() == 0

    def test_self_repair_is_visible_on_the_task_it_unblocked(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error=self.invalid_pair)

        requeue_transient_failed()

        task.refresh_from_db()
        assert "[self-repaired] agent_harness=pydantic_ai" in task.execution_reason

    def test_a_recurrence_after_the_one_repair_escalates_normally(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error=self.invalid_pair)
        requeue_transient_failed()

        Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED)
        _add_failed_attempt(task, error=self.invalid_pair)
        requeue_transient_failed()

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.count() == 1

    def test_a_genuine_decision_still_pages(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error="AssertionError: expected 3 got 4")

        requeue_transient_failed()

        assert DeferredQuestion.objects.count() == 1
