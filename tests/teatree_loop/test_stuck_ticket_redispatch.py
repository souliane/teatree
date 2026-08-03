"""Re-dispatch of stuck non-terminal tickets — the drain, hard-bounded (PR-5, #3958).

Two populations are drained by one sweep. A **frozen** ticket has ZERO open tasks,
no open PR, and no recent activity: the FSM reads a work-state but nothing is
scheduled to advance it. A **failing** ticket is not idle at all — its latest
attempt for the implied phase failed and nothing is in flight, so it churns rather
than stops. Both classes cover author AND reviewer roles (the failing population is
dominated by the ``reviewing`` phase, which lives on reviewer tickets), and both run
through the SAME #2009 repair budget, escalating LOUDLY via a ``DeferredQuestion``
when the budget is exhausted — so a stuck ticket is drained or surfaced, never silent.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from teatree.core.models import PullRequest, Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.transition import TicketTransition
from teatree.core.repair_loop import max_phase_iterations
from teatree.llm.anthropic_limits import LimitCause, LimitMatch
from teatree.loop.stuck_ticket_redispatch import (
    DEFAULT_STUCK_IDLE_HOURS,
    _idle_threshold_hours,
    redispatch_stuck_tickets,
)
from teatree.loop.tick_recovery import _reap_stale_task_claims


def _stuck_ticket(*, state: str = Ticket.State.STARTED, idle_hours: int = 48) -> Ticket:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state)
    transition = TicketTransition.objects.create(ticket=ticket, from_state="scoped", to_state=state)
    TicketTransition.objects.filter(pk=transition.pk).update(
        created_at=timezone.now() - timedelta(hours=idle_hours),
    )
    return ticket


def _finished_task(
    ticket: Ticket,
    *,
    phase: str,
    status: str,
    error: str = "",
    hours_ago: int = 0,
) -> Task:
    """A terminal task of *ticket* carrying one recorded attempt, aged *hours_ago*."""
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    task = Task.objects.create(ticket=ticket, session=session, phase=phase, status=status)
    attempt = TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=1 if error else 0,
        error=error,
    )
    if hours_ago:
        TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - timedelta(hours=hours_ago))
    return task


class TestStuckTicketRedispatch(TestCase):
    def test_stuck_started_ticket_schedules_planning(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.STARTED)

        scheduled = redispatch_stuck_tickets()

        assert scheduled == 1
        planning = ticket.tasks.filter(phase="planning", status=Task.Status.PENDING)
        assert planning.count() == 1

    def test_stuck_planned_ticket_schedules_coding(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.PLANNED)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="coding", status=Task.Status.PENDING).count() == 1

    def test_stuck_coded_ticket_schedules_testing(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.CODED)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).count() == 1

    def test_stuck_tested_ticket_schedules_reviewing(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.TESTED)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="reviewing", status=Task.Status.PENDING).count() == 1

    def test_stuck_reviewed_ticket_schedules_shipping(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.REVIEWED)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="shipping", status=Task.Status.PENDING).count() == 1

    def test_ticket_with_no_activity_record_is_left_alone(self) -> None:
        # No transition and no task means idleness cannot be measured; the sweep
        # must not re-dispatch a ticket it cannot prove is stale.
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.count() == 0

    def test_scheduling_refusal_is_escalated(self) -> None:
        _stuck_ticket(state=Ticket.State.STARTED)

        with patch.object(Ticket, "schedule_planning", side_effect=InvalidTransitionError("gate refused")):
            scheduled = redispatch_stuck_tickets()

        assert scheduled == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_poison_ticket_does_not_abort_the_sweep(self) -> None:
        # #3441: one stuck ticket whose per-item processing raises an unexpected
        # exception must NOT abort the sweep and strand every OTHER stuck ticket. The
        # poison ticket is skipped (logged, left as-is), the healthy one still schedules.
        poison = _stuck_ticket(state=Ticket.State.STARTED)  # created first ⇒ processed first
        healthy = _stuck_ticket(state=Ticket.State.STARTED)

        def _raise_on_poison(ticket: Ticket, *, phase: str) -> str | None:
            if ticket.pk == poison.pk:
                msg = "budget query blew up on the poison ticket"
                raise ValueError(msg)
            return None

        with patch(
            "teatree.loop.stuck_ticket_redispatch._budget_halt_reason",
            side_effect=_raise_on_poison,
        ):
            scheduled = redispatch_stuck_tickets()

        assert scheduled == 1  # the healthy ticket was scheduled despite the poison one
        assert healthy.tasks.filter(phase="planning", status=Task.Status.PENDING).count() == 1
        assert poison.tasks.count() == 0  # the poison ticket is skipped, never fatal

    def test_bad_idle_threshold_setting_falls_back_to_default(self) -> None:
        with override_settings(STUCK_TICKET_IDLE_HOURS="not-a-number"):
            assert _idle_threshold_hours() == DEFAULT_STUCK_IDLE_HOURS

    def test_ticket_with_an_open_task_is_left_alone(self) -> None:
        ticket = _stuck_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="planning")
        Task.objects.create(ticket=ticket, session=session, phase="planning", status=Task.Status.PENDING)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(phase="planning").count() == 1

    def test_recently_active_ticket_is_left_alone(self) -> None:
        ticket = _stuck_ticket(idle_hours=0)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.count() == 0

    def test_terminal_ticket_is_left_alone(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.MERGED)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.count() == 0

    def test_ticket_with_an_open_pr_is_left_alone(self) -> None:
        ticket = _stuck_ticket()
        ticket.pull_requests.create(url="https://ex.com/pr/1", repo="acme/app", iid="1")

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.count() == 0

    def test_budget_exhausted_ticket_is_escalated_not_scheduled(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.STARTED)
        cap = max_phase_iterations()
        # A run of prior distinct planning-phase failures burns the repair budget.
        for i in range(cap):
            session = Session.objects.create(ticket=ticket, agent_id="planning")
            task = Task.objects.create(ticket=ticket, session=session, phase="planning", status=Task.Status.FAILED)
            attempt = TaskAttempt.objects.create(
                task=task,
                execution_target=task.execution_target,
                ended_at=timezone.now(),
                exit_code=1,
                error=f"planning failed run {'x' * (i + 1)}",
            )
            # The failures are stale — the ticket has been sitting since they ran.
            TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - timedelta(hours=48))

        scheduled = redispatch_stuck_tickets()

        assert scheduled == 0
        assert ticket.tasks.filter(phase="planning", status=Task.Status.PENDING).count() == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1
        # Idempotent: a second sweep neither schedules nor spams another escalation.
        assert redispatch_stuck_tickets() == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_repeated_exhaustion_failures_do_not_halt_redispatch(self) -> None:
        # A stuck ticket whose recent phase attempts all died on the session limit must
        # NOT be treated as a stalled/doomed phase: usage-limit failures are capacity
        # dips, not defects, so they must not burn the repair budget nor trip the
        # identical-failure stall. The ticket is re-dispatched, never escalated.
        ticket = _stuck_ticket(state=Ticket.State.STARTED)
        err = LimitMatch(phrase="5-hour limit", cause=LimitCause.SUBSCRIPTION_SESSION).as_reason()
        for _ in range(2):
            session = Session.objects.create(ticket=ticket, agent_id="planning")
            task = Task.objects.create(ticket=ticket, session=session, phase="planning", status=Task.Status.FAILED)
            attempt = TaskAttempt.objects.create(
                task=task,
                execution_target=task.execution_target,
                ended_at=timezone.now(),
                exit_code=1,
                error=err,
            )
            TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - timedelta(hours=48))

        scheduled = redispatch_stuck_tickets()

        assert scheduled == 1
        assert ticket.tasks.filter(phase="planning", status=Task.Status.PENDING).count() == 1
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def _burn_planning_budget(self, ticket: Ticket) -> None:
        for i in range(max_phase_iterations()):
            session = Session.objects.create(ticket=ticket, agent_id="planning")
            task = Task.objects.create(ticket=ticket, session=session, phase="planning", status=Task.Status.FAILED)
            attempt = TaskAttempt.objects.create(
                task=task,
                execution_target=task.execution_target,
                ended_at=timezone.now(),
                exit_code=1,
                error=f"planning failed run {'x' * (i + 1)}",
            )
            TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - timedelta(hours=48))

    def test_answered_escalation_does_not_re_escalate(self) -> None:
        # #6: an escalated stuck ticket is parked. Answering the question must NOT spawn
        # a fresh escalation on the next tick (the old open-only dedup re-fired here).
        ticket = _stuck_ticket(state=Ticket.State.STARTED)
        self._burn_planning_budget(ticket)
        assert redispatch_stuck_tickets() == 0
        question = DeferredQuestion.objects.get()
        question.answered_at = timezone.now()
        question.save(update_fields=["answered_at"])

        assert redispatch_stuck_tickets() == 0
        assert DeferredQuestion.objects.count() == 1  # no re-escalation after the answer

    def test_wired_into_tick_recovery(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.STARTED)

        _reap_stale_task_claims()

        assert ticket.tasks.filter(phase="planning", status=Task.Status.PENDING).count() == 1


class TestReviewerRoleCandidates(TestCase):
    """#3958 gap 1: reviewer-role tickets were excluded outright by a ``role=author`` filter."""

    def _reviewer_ticket(self) -> Ticket:
        # A reviewer ticket is minted at NOT_STARTED and stays there until REVIEW_POSTED,
        # so no author state→phase mapping can name its implied phase.
        return Ticket.objects.create(role=Ticket.Role.REVIEWER, issue_url="https://ex.com/org/app/pull/7")

    def test_reviewer_ticket_with_a_failed_review_is_redispatched(self) -> None:
        ticket = self._reviewer_ticket()
        _finished_task(ticket, phase="reviewing", status=Task.Status.FAILED, error="result_error: envelope missing")

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="reviewing", status=Task.Status.PENDING).count() == 1
        assert DeferredQuestion.objects.count() == 0

    def test_reviewer_ticket_whose_review_completed_but_never_landed_is_redispatched(self) -> None:
        # The frozen half of the widening: the review task finished cleanly yet the ticket
        # never reached REVIEW_POSTED, and nothing is scheduled to advance it.
        ticket = self._reviewer_ticket()
        _finished_task(ticket, phase="reviewing", status=Task.Status.COMPLETED, hours_ago=48)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="reviewing", status=Task.Status.PENDING).count() == 1

    def test_reviewer_ticket_keeps_its_own_review_phase(self) -> None:
        # A codex reviewer ticket's phase encodes the review variant; re-dispatch must
        # re-schedule THAT phase, never collapse it to plain ``reviewing``.
        ticket = self._reviewer_ticket()
        _finished_task(
            ticket, phase="codex_reviewing", status=Task.Status.FAILED, error="no_result_envelope: nothing emitted"
        )

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="codex_reviewing", status=Task.Status.PENDING).count() == 1
        assert ticket.tasks.filter(phase="reviewing").count() == 0

    def test_reviewer_ticket_is_redispatched_despite_its_open_pr(self) -> None:
        # A reviewer ticket's PR is its SUBJECT, not its work in flight — every one has
        # an open PR by definition, so an open-PR exclusion would re-narrow the sweep
        # straight back to author-only and the widening would be inert in production.
        ticket = self._reviewer_ticket()
        ticket.pull_requests.create(url="https://ex.com/org/app/pull/7", repo="org/app", iid="7")
        _finished_task(ticket, phase="reviewing", status=Task.Status.FAILED, error="result_error: no verdict")

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="reviewing", status=Task.Status.PENDING).count() == 1

    def test_reviewer_ticket_with_an_open_review_task_is_left_alone(self) -> None:
        ticket = self._reviewer_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="reviewing")
        Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.PENDING)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(phase="reviewing").count() == 1

    def test_reviewer_ticket_that_never_ran_is_left_alone(self) -> None:
        # No task at all means no phase to imply and no failure to repair — the sweep
        # must not invent work for a reviewer ticket it cannot prove is stuck.
        ticket = self._reviewer_ticket()

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.count() == 0

    def test_review_posted_reviewer_ticket_is_left_alone(self) -> None:
        ticket = self._reviewer_ticket()
        ticket.state = Ticket.State.REVIEW_POSTED
        ticket.save(update_fields=["state"])
        _finished_task(ticket, phase="reviewing", status=Task.Status.FAILED, error="stale failure", hours_ago=48)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0

    def test_reviewer_ticket_at_the_iteration_cap_is_escalated_not_scheduled(self) -> None:
        ticket = self._reviewer_ticket()
        for i in range(max_phase_iterations()):
            # Distinct ENVIRONMENTAL reasons, so only the cap — not a stall — can halt it.
            _finished_task(
                ticket,
                phase="reviewing",
                status=Task.Status.FAILED,
                error=f"outage_death: unable to connect to api at host-{'z' * (i + 1)}",
            )

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1


class TestFailingCandidates(TestCase):
    """#3958 gap 2: the sweep keyed on frozen, so a ticket that keeps FAILING never matched."""

    def test_recently_failed_ticket_is_redispatched_though_not_idle(self) -> None:
        # Fails, gets re-dispatched, fails again: churning, never idle, so the
        # idle-threshold predicate alone can never reach it.
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        _finished_task(ticket, phase="testing", status=Task.Status.FAILED, error="outage_death: connection refused")

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).count() == 1

    def test_recently_completed_phase_is_not_a_failing_candidate(self) -> None:
        # A clean, recent attempt is neither frozen nor failing — nothing to repair.
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        _finished_task(ticket, phase="testing", status=Task.Status.COMPLETED)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0

    def test_repeated_deterministic_failures_are_escalated_not_redispatched(self) -> None:
        # The storm case: re-dispatching into a failure that is the work's own defect
        # reproduces it. The recorded FailureKind (#3957) is what makes the two
        # comparable — their free-text fingerprints differ, so the fingerprint stall
        # alone would keep re-running them until the cap.
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        for suffix in ("alpha", "beta"):
            _finished_task(
                ticket,
                phase="testing",
                status=Task.Status.FAILED,
                error=f"no_result_envelope: agent produced prose only in {suffix}",
            )

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_repeated_environmental_failures_are_redispatched_within_the_cap(self) -> None:
        # A transient/environmental failure is the environment's fault, not the work's,
        # so it stays retryable — bounded by the iteration cap, never by the kind stall.
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        for suffix in ("alpha", "beta"):
            _finished_task(
                ticket,
                phase="testing",
                status=Task.Status.FAILED,
                error=f"outage_death: unable to connect to api at {suffix}",
            )

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).count() == 1
        assert DeferredQuestion.objects.count() == 0

    def test_repeated_unclassified_failures_are_not_a_named_cause_stall(self) -> None:
        # ``unclassified`` is the ABSENCE of a name, so two unrelated failures both land
        # there. Treating that as one repeating defect would halt on a coincidence — the
        # text-based fingerprint stall is what compares those two.
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        for suffix in ("disk full on alpha", "assertion tripped in beta"):
            _finished_task(ticket, phase="testing", status=Task.Status.FAILED, error=suffix)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).count() == 1

    def test_a_deterministic_failure_followed_by_a_clean_run_is_not_a_stall(self) -> None:
        # Only two CONSECUTIVE named deterministic failures halt: a clean attempt after
        # them breaks the run, so the drain the sweep already performed still happens.
        ticket = _stuck_ticket(state=Ticket.State.CODED)
        for suffix in ("alpha", "beta"):
            _finished_task(
                ticket,
                phase="testing",
                status=Task.Status.FAILED,
                error=f"no_result_envelope: {suffix}",
                hours_ago=48,
            )
        _finished_task(ticket, phase="testing", status=Task.Status.COMPLETED, hours_ago=48)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).count() == 1

    def test_a_failed_phase_whose_output_landed_is_not_a_failing_candidate(self) -> None:
        # The already-done redispatch flood (3366/3336/3352): a shipping task that lost
        # its lease AFTER opening the PR is a dead artifact, not a failing phase. The
        # frozen class had to wait out the idle threshold to reach it; the failing class
        # would fire on the very next tick, so the landing check has to sit here.
        ticket = _stuck_ticket(state=Ticket.State.REVIEWED, idle_hours=0)
        _finished_task(ticket, phase="shipping", status=Task.Status.FAILED, error="stuck_loop: lease lost")
        ticket.pull_requests.create(
            url="https://ex.com/o/a/pull/9", repo="o/a", iid="9", state=PullRequest.State.MERGED
        )

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0

    def test_a_failed_phase_with_no_landing_evidence_is_still_a_candidate(self) -> None:
        # The control for the guard above: without it, a sweep that skipped every
        # shipping failure would pass that test for the wrong reason.
        ticket = _stuck_ticket(state=Ticket.State.REVIEWED, idle_hours=0)
        _finished_task(ticket, phase="shipping", status=Task.Status.FAILED, error="stuck_loop: lease lost")

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="shipping", status=Task.Status.PENDING).count() == 1

    def test_a_deterministic_shipping_failure_with_a_stray_pr_is_still_failing(self) -> None:
        # #3982's opposite direction: a landed PR proves SOME push succeeded, not that
        # THIS attempt's push gate refusal did. Only a LEASE_LOST failure may trust the
        # artifact half of the landing evidence; any other failure kind must still surface.
        ticket = _stuck_ticket(state=Ticket.State.REVIEWED, idle_hours=0)
        _finished_task(
            ticket, phase="shipping", status=Task.Status.FAILED, error="result_error: the push gate refused the branch"
        )
        ticket.pull_requests.create(
            url="https://ex.com/o/a/pull/9", repo="o/a", iid="9", state=PullRequest.State.MERGED
        )

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="shipping", status=Task.Status.PENDING).count() == 1


class TestEveryClassPassesTheBudget(TestCase):
    """#3958 acceptance: no re-dispatch path reaches a scheduler without the repair budget.

    The widening's safety rests entirely on the budget being on the ONE path both
    classes and both roles take. A second path that scheduled directly would be the
    defect, and it would be invisible to the per-class tests above — each of those
    asserts a candidate IS drained, which a bypassing path satisfies just as well.
    Halting the budget for every candidate is what makes a bypass observable.
    """

    def _candidates_of_every_class(self) -> None:
        frozen_author = _stuck_ticket(state=Ticket.State.PLANNED)
        failing_author = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        _finished_task(failing_author, phase="testing", status=Task.Status.FAILED, error="outage_death: refused")
        for phase in ("reviewing", "codex_reviewing"):
            reviewer = Ticket.objects.create(role=Ticket.Role.REVIEWER, issue_url=f"https://ex.com/o/a/pull/{phase}")
            _finished_task(reviewer, phase=phase, status=Task.Status.FAILED, error="outage_death: refused")
        assert Ticket.objects.count() == 4
        assert frozen_author.tasks.count() == 0

    def test_a_halting_budget_stops_every_candidate_class(self) -> None:
        self._candidates_of_every_class()
        with patch(
            "teatree.loop.stuck_ticket_redispatch._budget_halt_reason",
            return_value="halted by the budget",
        ):
            assert redispatch_stuck_tickets() == 0

        assert Task.objects.filter(status__in=Task.Status.active()).count() == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 4

    def test_the_same_candidates_are_all_drained_when_the_budget_allows(self) -> None:
        # The control: without it, a sweep that silently found NO candidates would pass
        # the halting assertion above for the wrong reason.
        self._candidates_of_every_class()

        assert redispatch_stuck_tickets() == 4
        assert Task.objects.filter(status=Task.Status.PENDING).count() == 4
