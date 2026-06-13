from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import models, transaction
from django.utils import timezone
from django_fsm import FSMField

from teatree.core.managers import TaskManager
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.session import Session
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from teatree.core.cost import AttemptUsage, CostBreakdown


class Task(models.Model):
    class ExecutionTarget(models.TextChoices):
        HEADLESS = "headless", "Headless"
        INTERACTIVE = "interactive", "Interactive"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CLAIMED = "claimed", "Claimed"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="tasks")
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="tasks")
    parent_task = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_tasks",
    )
    phase = models.CharField(max_length=64, blank=True)
    execution_target = models.CharField(
        max_length=32,
        choices=ExecutionTarget.choices,
        default=ExecutionTarget.HEADLESS,
    )
    execution_reason = models.TextField(blank=True)
    status = FSMField(max_length=32, choices=Status.choices, default=Status.PENDING)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_by = models.CharField(max_length=255, blank=True)
    claimed_by_session = models.CharField(max_length=255, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    result_artifact_path = models.CharField(max_length=500, blank=True)
    # #129 TODO-sweep idempotency stamp. The sweep scanner marks a task
    # checked via an atomic conditional UPDATE before verifying its artifact,
    # so two concurrent ticks never double-verify (or double-complete) the
    # same task. Null = never swept.
    last_sweep_check_ts = models.DateTimeField(null=True, blank=True)

    objects = TaskManager()

    class Meta:
        db_table = "teatree_task"

    def __str__(self) -> str:
        return f"task-{self.pk}-{self.execution_target!s}"

    def save(self, *args: object, **kwargs: object) -> None:
        if self._state.adding and self.execution_target == self.ExecutionTarget.HEADLESS:
            self._default_loop_dispatched_to_interactive()
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def loop_dispatched(cls, *, role: str, phase: str) -> bool:
        """True iff ``(role, phase)`` has a registered phase sub-agent.

        Such a task is dispatched per-phase by the in-session ``/loop`` slot
        (``loop_dispatch claim-next`` → the ``Agent`` tool), never via a
        detached headless-SDK run. Post the 2026-06-15 billing change
        a detached headless-SDK dispatch is metered, so a loop-dispatched phase
        task must run INTERACTIVE (subscription-covered). A pair with no
        registered agent is free-form headless work and is left HEADLESS.
        """
        from teatree.core.phases import subagent_for_phase  # noqa: PLC0415

        return bool(subagent_for_phase(role, phase))

    def _default_loop_dispatched_to_interactive(self) -> None:
        """Route a freshly-created loop-dispatched phase task to INTERACTIVE.

        The single chokepoint for "phase tasks default to interactive": the
        loop is their sole dispatcher, so every ``schedule_*`` / scanner / CLI
        creation site inherits the rule here without each having to know it.
        Only an insert-time HEADLESS row is touched; an explicit
        ``route_to_interactive`` / ``route_to_headless`` after creation goes
        through ``_route`` (not an insert) and is never overridden here.
        """
        try:
            role = self.ticket.role
        except Task.ticket.RelatedObjectDoesNotExist:
            return
        if not self.loop_dispatched(role=role, phase=self.phase):
            return
        self.execution_target = self.ExecutionTarget.INTERACTIVE
        if not self.execution_reason:
            self.execution_reason = "Loop-dispatched phase — in-session sub-agent (subscription-covered)"

    def claim(self, *, claimed_by: str, lease_seconds: int = 300) -> None:
        now = timezone.now()
        with transaction.atomic():
            locked = Task.objects.select_for_update().get(pk=self.pk)
            if locked.status in {self.Status.COMPLETED, self.Status.FAILED}:
                msg = "Task already finished"
                raise InvalidTransitionError(msg)
            if locked.status == self.Status.CLAIMED and locked.lease_expires_at and locked.lease_expires_at > now:
                msg = "Task already claimed"
                raise InvalidTransitionError(msg)
            locked.status = self.Status.CLAIMED
            locked.claimed_by = claimed_by
            locked.claimed_at = now
            locked.heartbeat_at = now
            locked.lease_expires_at = now + timedelta(seconds=lease_seconds)
            locked.save(
                update_fields=[
                    "status",
                    "claimed_by",
                    "claimed_at",
                    "heartbeat_at",
                    "lease_expires_at",
                ],
            )
        self.refresh_from_db()

    def renew_lease(self, *, lease_seconds: int = 300) -> None:
        now = timezone.now()
        self.heartbeat_at = now
        self.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self.save(update_fields=["heartbeat_at", "lease_expires_at"])

    def route_to_headless(self, *, reason: str = "") -> None:
        self._route(self.ExecutionTarget.HEADLESS, reason)

    def route_to_interactive(self, *, reason: str = "") -> None:
        self._route(self.ExecutionTarget.INTERACTIVE, reason)

    def complete(self, *, result_artifact_path: str = "") -> None:
        """Mark the task COMPLETED and auto-advance the ticket — atomically.

        #883: the task ``save()`` and the FSM transition in
        ``_advance_ticket`` are wrapped in a single ``transaction.atomic``.
        Pre-#883 these were two separate write boundaries: a crash between
        them left the task COMPLETED but the ticket on its old state, and
        because the task is no longer CLAIMED neither ``reap_stale_claims``
        nor ``reclaim_orphaned_claims`` could rescue it — the loop stalled
        forever. One transaction closes that window: either both writes
        land or neither does. ``replay_orphaned_transitions`` is the
        boot/tick safety net for rows that slipped through before the fix
        or any future seam.
        """
        with transaction.atomic():
            self.status = self.Status.COMPLETED
            self.result_artifact_path = result_artifact_path
            self._clear_claim()
            self.save(
                update_fields=[
                    "status",
                    "result_artifact_path",
                    "claimed_at",
                    "claimed_by",
                    "lease_expires_at",
                    "heartbeat_at",
                ],
            )
            self._advance_ticket()

    def complete_surfacing_advance_failure(self, *, result_artifact_path: str = "") -> str:
        """Complete the task; on a TYPED FSM-advance refusal, keep the task done.

        The operator out-of-band-done path (``tasks complete``, #1977): a
        deliberate gate refusal during the auto-advance — a ``planning`` task on
        a ticket with no ``PlanArtifact`` (``NoPlanArtifactError``), a dirty
        worktree (``DirtyWorktreeError``), a missing shipping attestation
        (``QualityGateError``), or any ``TransitionNotAllowed`` — must NOT wedge
        the task ``claimed`` by rolling back the completion. The task-completion
        bookkeeping commits in its OWN boundary, then the FSM advance runs in a
        SEPARATE one; a typed refusal there is returned (caller surfaces it
        loudly) instead of propagating to roll back the completion. The
        ``replay_orphaned_transitions`` boot/tick sweep fires the transition
        later once the gate is satisfied. Returns ``""`` on a clean advance,
        else the refusal reason.

        ``complete()`` keeps its #883 single-atomic coupling for the loop /
        headless callers; this is the deliberate operator-only decoupling.
        """
        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        from teatree.core.models.errors import QualityGateError  # noqa: PLC0415

        with transaction.atomic():
            self.status = self.Status.COMPLETED
            self.result_artifact_path = result_artifact_path
            self._clear_claim()
            self.save(
                update_fields=[
                    "status",
                    "result_artifact_path",
                    "claimed_at",
                    "claimed_by",
                    "lease_expires_at",
                    "heartbeat_at",
                ],
            )
        try:
            self._advance_ticket()
        except (InvalidTransitionError, QualityGateError, TransitionNotAllowed) as exc:
            return str(exc) or exc.__class__.__name__
        return ""

    def _advance_ticket(self) -> None:
        """Auto-advance ticket state based on the completed task's phase.

        Each phase's completion triggers the matching FSM transition, which in
        turn auto-schedules the next-phase task via the ``schedule_*`` methods
        on ``Ticket``. The guards on ``self.phase`` + ``ticket.state`` make
        this safe for repeat calls (e.g. parallel child tasks): once a ticket
        has advanced, later calls find the state mismatch and no-op.
        """
        if self._last_attempt_needs_user_input():
            self._schedule_interactive_followup()
            return
        self._record_phase_visit()
        self._apply_phase_transition()

    def _needs_user_input_followup_pending(self) -> bool:
        """True iff this task was *held* for human input (#927).

        The agent returned ``needs_user_input`` so ``_advance_ticket``
        deliberately did NOT fire the FSM transition and scheduled an
        interactive followup instead. The replay sweep
        (``replay_orphaned_transitions``) takes this task as
        latest-per-ticket and would otherwise force-advance the ticket
        past a phase the agent said it could not finish, orphaning the
        followup. The suppression therefore belongs on the *shared*
        transition path, not only the live ``complete()`` chain.
        """
        return self._last_attempt_needs_user_input()

    def _apply_phase_transition(self) -> bool:
        """Fire the FSM transition this task's phase implies, if its guard holds.

        The single phase→state advance path, shared by the live
        ``complete()`` chain and the ``replay_orphaned_transitions``
        boot/tick recovery sweep (#883) — there is exactly ONE place that
        maps a completed phase to an FSM transition, so replay can never
        skip a lifecycle gate the live path enforces. Every branch is
        guarded by both ``phase`` *and* the required ``ticket.state``
        (gate-integrity): a ``shipping`` task whose ticket never went
        through code→test→review finds no matching guard and no-ops, so a
        ticket can never reach a state it did not earn. The guards also
        make the call idempotent — once the ticket has advanced, a repeat
        call (parallel child task, or a replay of an already-applied
        transition) finds the state mismatch and no-ops.

        A task held for human input (#927) never fires its transition
        here — the agent said it could not finish this phase, so neither
        the live ``complete()`` chain nor the replay sweep may advance
        the ticket past it. Enforced on this shared path so the gate is
        not bypassable by any caller of ``_apply_phase_transition``.

        Returns ``True`` iff a transition fired (used by the replay sweep
        to count recovered tickets).
        """
        if self._needs_user_input_followup_pending():
            return False
        ticket = self.ticket
        ticket.refresh_from_db()
        # Normalize once, mirroring _record_phase_visit() — a task whose
        # phase is a short verb ("review"/"code"/...) must advance the
        # FSM too, not just record the session visit (#750). Raw
        # comparison silently desynced ticket.state from visited_phases.
        from teatree.core.phases import normalize_phase  # noqa: PLC0415

        phase = normalize_phase(self.phase)
        # Mirror the FSM source list of mark_reviewed_externally() — guarding
        # only on ``role == REVIEWER`` is not enough (#1000): the #998/#999
        # orphan sweep can complete a second reviewing task on a ticket that
        # already advanced to DELIVERED (or any other terminal state), and an
        # unconditional FSM call then raises TransitionNotAllowed and crashes
        # the loop tick. Sibling branches below all guard on ``ticket.state``;
        # this branch must too. The states enumerated here are exactly the
        # ``source=[...]`` argument of ``mark_reviewed_externally`` — keep
        # them in sync if that list ever changes.
        mark_reviewed_externally_source_states = {
            Ticket.State.NOT_STARTED,
            Ticket.State.SCOPED,
            Ticket.State.STARTED,
            Ticket.State.PLANNED,
            Ticket.State.CODED,
            Ticket.State.TESTED,
            Ticket.State.REVIEWED,
        }
        with transaction.atomic():
            if (
                phase == "reviewing"
                and ticket.role == Ticket.Role.REVIEWER
                and ticket.state in mark_reviewed_externally_source_states
            ):
                ticket.mark_reviewed_externally()
                ticket.save()
            elif phase == "scoping" and ticket.state == Ticket.State.SCOPED:
                ticket.start()
                ticket.save()
            elif phase == "planning" and ticket.state == Ticket.State.STARTED:
                ticket.plan()
                ticket.save()
            elif phase == "coding" and ticket.state == Ticket.State.PLANNED:
                ticket.code()
                ticket.save()
            elif phase == "testing" and ticket.state == Ticket.State.CODED:
                ticket.test(passed=True)
                ticket.save()
            elif phase == "reviewing" and ticket.state == Ticket.State.TESTED:
                ticket.review()
                ticket.save()
            elif phase == "shipping" and ticket.state == Ticket.State.REVIEWED:
                # #1284 (codex #1282-2): the task-based completion path must
                # enforce the same visited-phases gate the ``pr create`` path
                # runs through ``_check_shipping_gate`` — otherwise a REVIEWED
                # ticket with missing testing/reviewing attestations advances
                # to SHIPPED through the task path, bypassing the gate. The
                # single source of truth is ``Session.visited_phases`` union
                # across the ticket (#694); ``check_gate_across_ticket``
                # raises ``QualityGateError`` when phases are missing, which
                # propagates out of ``_apply_phase_transition`` so the caller
                # surfaces the structured failure rather than silently
                # advancing the FSM.
                self.session.check_gate_across_ticket("shipping")
                ticket.ship()
                ticket.save()
            else:
                return False
        return True

    def _record_phase_visit(self) -> None:
        """Record this task's phase on its session as completion happens (#694).

        Couples the FSM to the work: finishing a phase task *is* the phase
        visit, so the shipping gate's single source of truth
        (``Session.visited_phases``) is fed by the loop path without a
        separate ``lifecycle visit-phase`` CLI call. The phase is normalized
        so the loop path and the CLI path write the same canonical token.
        """
        from teatree.core.phases import normalize_phase  # noqa: PLC0415

        if not self.phase:
            return
        # #755: resolve a guaranteed-non-empty attribution identity,
        # symmetric with the CLI path — a blank Session.agent_id must not
        # silently drop the maker attribution here either.
        self.session.visit_phase(
            normalize_phase(self.phase),
            agent_id=self.session.recording_identity(),
        )

    def _last_attempt_needs_user_input(self) -> bool:
        last = self.attempts.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
        return bool(last and isinstance(last.result, dict) and last.result.get("needs_user_input"))

    def _schedule_interactive_followup(self) -> "Task":
        """Create a new interactive task for human handoff, carrying the headless session_id."""
        last = self.attempts.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
        reason = str(last.result.get("user_input_reason", "Agent needs human input")) if last else "Agent needs input"
        agent_session_id = last.agent_session_id if last else ""
        session = Session.objects.create(
            ticket=self.ticket,
            agent_id=agent_session_id or "interactive-followup",
        )
        return Task.objects.create(
            ticket=self.ticket,
            session=session,
            phase=self.phase,
            execution_target=self.ExecutionTarget.INTERACTIVE,
            execution_reason=reason,
            parent_task=self,
        )

    def fail(self) -> None:
        self.status = self.Status.FAILED
        self._clear_claim()
        self.save(update_fields=["status", "claimed_at", "claimed_by", "lease_expires_at", "heartbeat_at"])

    def reopen(self) -> None:
        if self.status != self.Status.FAILED:
            msg = f"Can only reopen failed tasks, got '{self.status}'"
            raise InvalidTransitionError(msg)
        self.status = self.Status.PENDING
        self.save(update_fields=["status"])

    def complete_with_attempt(
        self,
        *,
        artifact_path: str = "",
        exit_code: int = 0,
        error: str = "",
        result: dict[str, object] | None = None,
    ) -> "TaskAttempt":
        attempt = TaskAttempt.objects.create(
            task=self,
            execution_target=self.execution_target,
            ended_at=timezone.now(),
            exit_code=exit_code,
            artifact_path=artifact_path,
            error=error,
            result=result or {},
        )
        if exit_code == 0:
            self.complete(result_artifact_path=artifact_path)
        else:
            self.fail()
        return attempt

    def spawn_child_tasks(self, repos: list[str], *, phase: str = "") -> list["Task"]:
        """Create one child task per repo for parallel execution.

        Each child task inherits the ticket and session from the parent.
        The parent can wait for all children by querying ``child_tasks``.
        """
        children = []
        for repo in repos:
            child = Task.objects.create(
                ticket=self.ticket,
                session=self.session,
                phase=phase or self.phase,
                execution_target=self.execution_target,
                execution_reason=f"Repo: {repo}",
                parent_task=self,
            )
            children.append(child)
        return children

    def all_children_done(self) -> bool:
        """Return True if all child tasks have reached a terminal state."""
        children = self.child_tasks.all()  # ty: ignore[unresolved-attribute]
        if not children.exists():
            return True
        return not children.exclude(status__in={self.Status.COMPLETED, self.Status.FAILED}).exists()

    def _route(self, target: ExecutionTarget, reason: str) -> None:
        self.execution_target = target
        self.execution_reason = reason
        self.status = self.Status.PENDING
        self._clear_claim()
        self.save(
            update_fields=[
                "execution_target",
                "execution_reason",
                "status",
                "claimed_at",
                "claimed_by",
                "lease_expires_at",
                "heartbeat_at",
            ],
        )

    def _clear_claim(self) -> None:
        self.claimed_at = None
        self.claimed_by = ""
        self.claimed_by_session = ""
        self.lease_expires_at = None
        self.heartbeat_at = None


class TaskAttemptQuerySet(models.QuerySet):
    def headless(self) -> "TaskAttemptQuerySet":
        """Only the attempts that ran a billed detached headless-SDK run.

        SDK-equivalent billing covers headless usage only — interactive turns
        run inside the user's own session, not against the credit.
        """
        return self.filter(execution_target=Task.ExecutionTarget.HEADLESS)

    def usages(self) -> "list[AttemptUsage]":
        """Map each attempt to the :class:`AttemptUsage` the cost layer reads."""
        from teatree.core.cost import AttemptUsage  # noqa: PLC0415

        return [
            AttemptUsage(
                model=row.model or None,
                reported_cost_usd=row.cost_usd,
                input_tokens=row.input_tokens or 0,
                output_tokens=row.output_tokens or 0,
                cache_read_tokens=row.cache_read_tokens or 0,
                cache_write_tokens=row.cache_write_tokens or 0,
            )
            for row in self.only(
                "model",
                "cost_usd",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            )
        ]

    def cost_breakdown(self) -> "CostBreakdown":
        """SDK-equivalent spend across the attempts in this queryset."""
        from teatree.core.cost import CostBreakdown  # noqa: PLC0415

        return CostBreakdown.from_usages(self.usages())


class TaskAttempt(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attempts")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    execution_target = models.CharField(max_length=32, choices=Task.ExecutionTarget.choices)
    error = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    artifact_path = models.CharField(max_length=500, blank=True)
    result = models.JSONField(default=dict, blank=True)
    model = models.CharField(max_length=128, blank=True)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    cache_read_tokens = models.IntegerField(null=True, blank=True)
    cache_write_tokens = models.IntegerField(null=True, blank=True)
    cost_usd = models.FloatField(null=True, blank=True)
    num_turns = models.IntegerField(null=True, blank=True)
    launch_url = models.URLField(max_length=500, blank=True)
    agent_session_id = models.CharField(max_length=255, blank=True)

    objects = TaskAttemptQuerySet.as_manager()

    class Meta:
        db_table = "teatree_taskattempt"

    def __str__(self) -> str:
        return f"attempt-{self.pk or 'new'!s}"
