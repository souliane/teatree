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

from teatree.agents.envelope_refusal import NO_ENVELOPE_ERROR
from teatree.core.modelkit.task_failure_taxonomy import CANCELLED_PREFIX
from teatree.core.models import PullRequest, Session, Task, TaskAttempt, Ticket
from teatree.core.models.auto_review_dispatch import AutoReviewDispatch
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.models.transition import TicketTransition
from teatree.core.repair_loop import max_phase_iterations
from teatree.llm.anthropic_limits import LimitCause, LimitMatch
from teatree.loop.stuck_ticket_redispatch import (
    DEFAULT_STUCK_IDLE_HOURS,
    _idle_threshold_hours,
    redispatch_stuck_tickets,
)
from teatree.loop.tick_recovery import _reap_stale_task_claims

_REVIEWED_HEAD = "a1b2c3d4" * 5


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
        return Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url="https://ex.com/org/app/pull/7",
            extra={"reviewed_sha": _REVIEWED_HEAD},
        )

    def _recorded_verdict(self) -> None:
        # No ``ticket=``: the production recorders leave that FK unset, so the sweep has to
        # find the verdict by the PR the reviewer ticket names.
        ReviewVerdict.record(
            pr_id=7,
            slug="org/app",
            reviewed_sha=_REVIEWED_HEAD,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="cold-reviewer",
        )

    def test_a_lease_lost_review_that_recorded_its_verdict_is_not_redispatched(self) -> None:
        # The duplicate-dispatch half of #4100: reviews that had already produced their
        # verdict read as failed, so the sweep queued three concurrent tasks for one PR.
        ticket = self._reviewer_ticket()
        _finished_task(ticket, phase="reviewing", status=Task.Status.FAILED, error="stuck_loop: lease lost for task 1")
        self._recorded_verdict()

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0

    def test_the_dispatched_head_is_what_the_sweep_reads(self) -> None:
        # The #68 population the issue's tasks come from: the head is on the dispatch row,
        # and the ticket carries none at all.
        ticket = self._reviewer_ticket()
        ticket.merge_extra(pop_keys=["reviewed_sha"])
        task = _finished_task(
            ticket, phase="reviewing", status=Task.Status.FAILED, error="stuck_loop: lease lost for task 1"
        )
        AutoReviewDispatch.objects.create(slug="org/app", pr_id=7, head_sha=_REVIEWED_HEAD, task=task)
        self._recorded_verdict()

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0

    def test_a_lease_lost_review_that_recorded_nothing_is_still_redispatched(self) -> None:
        # The control for the guard above: without it, a sweep that skipped every
        # lease-lost review would pass that test for the wrong reason.
        ticket = self._reviewer_ticket()
        _finished_task(ticket, phase="reviewing", status=Task.Status.FAILED, error="stuck_loop: lease lost for task 1")

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="reviewing", status=Task.Status.PENDING).count() == 1

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
                error=f"missing required evidence for phase 'testing': no tests_run in {suffix}",
            )

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_repeated_causeless_failures_are_redispatched_within_the_cap(self) -> None:
        # #4075: ``no_result_envelope`` is the ABSENCE of a cause — the run reported
        # nothing — so two of them are one silence repeated, not one defect. Halting here
        # declared four real phases doomed that later ran the same phase and succeeded.
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        for suffix in ("alpha", "beta"):
            _finished_task(
                ticket,
                phase="testing",
                status=Task.Status.FAILED,
                error=f"no_result_envelope: agent produced prose only in {suffix}",
            )

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).count() == 1
        assert DeferredQuestion.objects.count() == 0

    def test_repeated_identical_causeless_failures_are_redispatched_too(self) -> None:
        # The real shape: the reason is a CONSTANT, so both attempts also share one
        # fingerprint — the FINGERPRINT stall, not just the kind stall, must let it through.
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        for _ in range(2):
            _finished_task(ticket, phase="testing", status=Task.Status.FAILED, error=NO_ENVELOPE_ERROR)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).count() == 1
        assert DeferredQuestion.objects.count() == 0

    def test_repeated_runtime_ceilings_are_redispatched_though_their_fingerprints_differ(self) -> None:
        # #4276: the sibling causeless kind, and the one the KIND-level drop actually
        # carries — the reason interpolates the breach, so the two fingerprint
        # differently and the fingerprint filter has nothing to drop. Only
        # ``_deterministic_kinds`` keeps this out of the two-strikes stall.
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        for seconds in (3601, 3722):
            _finished_task(
                ticket,
                phase="testing",
                status=Task.Status.FAILED,
                error=f"stuck_loop: runtime ceiling exceeded: ran {seconds}s without exiting",
            )

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).count() == 1
        assert DeferredQuestion.objects.count() == 0

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

    def test_two_verbatim_identical_environmental_failures_are_redispatched_not_frozen(self) -> None:
        # The text stall runs BEFORE the named-cause one, and compared every failure's
        # text — so an environmental fault that repeats VERBATIM (one harness traceback,
        # one unconfigured credential) froze the ticket at two, the exact halt the
        # named-cause check drops environmental kinds to refuse.
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        for _ in range(2):
            _finished_task(
                ticket,
                phase="testing",
                status=Task.Status.FAILED,
                error="outage_death: unable to connect to api",
            )

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).count() == 1
        assert DeferredQuestion.objects.count() == 0

    def test_two_verbatim_identical_deterministic_failures_still_halt(self) -> None:
        # The other side of the same edit: dropping only the environmental rows must not
        # weaken the text stall for a defect that reproduces verbatim.
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        for _ in range(2):
            _finished_task(
                ticket,
                phase="testing",
                status=Task.Status.FAILED,
                error="the review found a real defect in the diff",
            )

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

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
                error=f"missing required evidence for phase 'testing': {suffix}",
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


class TestOperatorCancelledTickets(TestCase):
    """A cancellation is a DECISION, and the drain must not undo it (#4105).

    Cancelling a task leaves exactly the shape both admission predicates match — a
    non-terminal ticket, nothing in flight, a failed newest attempt — so the sweep
    re-queued the same phase on the next tick and the operator's decline lasted one
    tick. Two cancels were needed to halt it, and only by exhausting the repair
    budget, which reports a doomed phase rather than an honoured decision.
    """

    def _cancelled_ticket(self, *, state: str = Ticket.State.CODED, idle_hours: int = 0) -> Ticket:
        ticket = _stuck_ticket(state=state, idle_hours=idle_hours)
        self._cancel(ticket)
        return ticket

    @staticmethod
    def _cancel(ticket: Ticket) -> None:
        """What `t3 <overlay> tasks cancel` leaves behind: a failed attempt plus a named task."""
        task = _finished_task(ticket, phase="testing", status=Task.Status.FAILED, error=f"{CANCELLED_PREFIX}not now")
        task.fail(reason=f"{CANCELLED_PREFIX}not now")

    def test_a_cancelled_phase_is_not_redispatched(self) -> None:
        ticket = self._cancelled_ticket()

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0

    def test_a_second_cancel_is_honoured_rather_than_escalated(self) -> None:
        """One cancel suffices, so the budget never runs out.

        Two cancels used to be what stopped the sweep, and only by exhausting the
        repair budget — which pages the operator with "this phase is doomed" about a
        phase they deliberately stopped.
        """
        ticket = self._cancelled_ticket()
        self._cancel(ticket)

        assert redispatch_stuck_tickets() == 0
        assert DeferredQuestion.objects.count() == 0

    def test_the_idle_class_does_not_reach_it_either(self) -> None:
        """Ageing past the idle threshold is not "something changed"."""
        ticket = self._cancelled_ticket(idle_hours=DEFAULT_STUCK_IDLE_HOURS * 8)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0

    def test_a_newer_failure_puts_the_ticket_back_in_play(self) -> None:
        """The NEWEST task decides — a cancel the pipeline moved past is not the last word."""
        ticket = self._cancelled_ticket()
        _finished_task(ticket, phase="testing", status=Task.Status.FAILED, error="outage_death: refused")

        assert redispatch_stuck_tickets() == 1

    def test_an_uncancelled_failure_of_the_same_shape_is_still_redispatched(self) -> None:
        """The control: without it, a sweep finding no candidates would pass for the wrong reason."""
        ticket = _stuck_ticket(state=Ticket.State.CODED, idle_hours=0)
        _finished_task(ticket, phase="testing", status=Task.Status.FAILED, error="outage_death: refused")

        assert redispatch_stuck_tickets() == 1

    def test_a_cancelled_reviewer_ticket_is_not_redispatched(self) -> None:
        """The guard is asked for EVERY live ticket, reviewer role included — not just author's."""
        ticket = Ticket.objects.create(role=Ticket.Role.REVIEWER, issue_url="https://ex.com/org/app/pull/9")
        task = _finished_task(ticket, phase="reviewing", status=Task.Status.FAILED, error=f"{CANCELLED_PREFIX}not now")
        task.fail(reason=f"{CANCELLED_PREFIX}not now")

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(status=Task.Status.PENDING).count() == 0
