from datetime import datetime
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone
from django_fsm import FSMField, TransitionNotAllowed

from teatree.core.managers import TaskManager
from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE, phase_spellings
from teatree.core.models.auto_implement import is_auto_implement
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.external_delivery import not_under_external_delivery_q
from teatree.core.models.session import Session
from teatree.core.models.task_claim import claim as _claim_task
from teatree.core.models.task_claim import renew_lease as _renew_task_lease
from teatree.core.models.task_phase_disposition import (
    dispose_unshippable_review,
    escalate_unmatched_phase_transition,
    transition_source_states,
)
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from teatree.core.models.task_attempt import TaskAttempt


class Task(models.Model):
    class ExecutionTarget(models.TextChoices):
        HEADLESS = "headless", "Headless"
        INTERACTIVE = "interactive", "Interactive"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CLAIMED = "claimed", "Claimed"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

        @classmethod
        def active(cls) -> frozenset["Task.Status"]:
            """The states a task is still being worked in — the active half of the partition."""
            return frozenset({cls.PENDING, cls.CLAIMED})

        @classmethod
        def terminal(cls) -> frozenset["Task.Status"]:
            """The states a task is finished in — the terminal half of the partition."""
            return frozenset({cls.COMPLETED, cls.FAILED})

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="tasks")
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="tasks")
    parent_task = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_tasks",
    )
    subject = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)
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
    # Directive #3 usage-window park gate. When a dispatch hits an exhausted usage
    # window (and ``limit_autorecovery_enabled`` is on) the task is returned to the
    # queue PENDING with ``not_before`` = the window's re-arm instant; the claim path
    # skips it until then, so a parked task never re-dispatches into the same 429. Null
    # (every task that was never limit-parked) leaves the claim path byte-identical.
    not_before = models.DateTimeField(null=True, blank=True)
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

    def display_subject(self) -> str:
        """A human-readable one-line description of the work this task is about.

        Prefers an explicitly stored ``subject``, then the work item the task
        targets (the ticket's terminal-friendly summary or its cached tracker
        title), and last the ``#N phase`` shape. Never returns the bare phase
        token alone — that is the unreadable ``Task NN (short_describe)`` the
        statusline used to show when nothing populated this field.
        """
        if self.subject.strip():
            return self.subject.strip()
        title = self.ticket.short_description or self._ticket_issue_title()
        number = self.ticket.ticket_number
        if title.strip():
            return f"#{number} {title.strip()}"
        if self.phase:
            return f"#{number} {self.phase}"
        return f"#{number}"

    def _ticket_issue_title(self) -> str:
        extra = self.ticket.extra if isinstance(self.ticket.extra, dict) else {}
        title = extra.get("issue_title", "")
        return title if isinstance(title, str) else ""

    @classmethod
    def loop_dispatched(cls, *, role: str, phase: str) -> bool:
        """True iff ``(role, phase)`` has a registered phase sub-agent.

        Pure registry membership (``SUBAGENT_BY_PHASE``). Whether such a task
        runs in-session or headless is the ``agent_runtime`` setting's call,
        resolved by ``headless_dispatch.runs_in_session``: under ``interactive``
        (default) it is dispatched per-phase by the in-session ``/loop`` slot
        (``loop_dispatch claim-next`` → the ``Agent`` tool); under a headless
        runtime it runs via ``agents/headless.py``. A pair with no registered
        agent is free-form headless work and always runs headless.
        """
        from teatree.core.modelkit.phases import subagent_for_phase  # noqa: PLC0415 — deferred: call-time import

        return bool(subagent_for_phase(role, phase))

    @staticmethod
    def dispatchable_q() -> Q:
        """The single filter selecting loop-DISPATCHABLE Tasks — the SSOT (#6).

        A Task is dispatchable when its ``(ticket.role, phase)`` pair has a
        registered sub-agent (``SUBAGENT_BY_PHASE``, matched across every accepted
        phase spelling via ``phase_spellings`` — the DB-side of ``loop_dispatched``)
        AND its ticket is NOT under a live #2104 external-delivery lease
        (``not_under_external_delivery_q``, #2217).

        The ONE source of truth every dispatch consumer builds on: the
        ``orchestrate`` planner's target + admit sweep and its in-flight budget
        count, and the live ``claim-next``/``pending-spawn`` in ``loop_dispatch``
        (which AND ``execution_target == INTERACTIVE`` on top). Because all sites
        reference this symbol, the external-delivery exclusion and the role/phase
        set can never diverge across them the way #2218's fix landed on one side.
        """
        role_phase = Q(pk__in=[])
        for role, phase in SUBAGENT_BY_PHASE:
            role_phase |= Q(ticket__role=role, phase__in=phase_spellings(phase))
        return role_phase & not_under_external_delivery_q()

    def _default_loop_dispatched_to_interactive(self) -> None:
        """Route a freshly-created loop-dispatched phase task to INTERACTIVE.

        The single chokepoint for "phase tasks default to interactive": when the
        ``agent_runtime`` setting selects ``interactive`` (the default) the loop
        is their sole dispatcher, so every ``schedule_*`` / scanner / CLI creation
        site inherits the rule here without each having to know it. Under
        ``agent_runtime=headless`` the row is left HEADLESS so the headless lane
        takes it. Only an insert-time HEADLESS row is touched; an explicit
        ``route_to_interactive`` / ``route_to_headless`` after creation goes
        through ``_route`` (not an insert) and is never overridden here.

        Mirrors ``headless_dispatch.runs_in_session`` (the predicate the signal /
        drain / refusal gates share). It is inlined here rather than called because
        ``core.models`` may not depend on the parent ``teatree.core`` node where
        ``headless_dispatch`` lives (tach); the ``teatree.config`` edge is allowed.
        """
        from teatree.config import AgentRuntime, get_effective_settings  # noqa: PLC0415 — deferred: call-time import

        try:
            role = self.ticket.role
        except Task.ticket.RelatedObjectDoesNotExist:
            return
        if get_effective_settings().agent_runtime is not AgentRuntime.INTERACTIVE:
            return
        if not self.loop_dispatched(role=role, phase=self.phase):
            return
        self.execution_target = self.ExecutionTarget.INTERACTIVE
        if not self.execution_reason:
            self.execution_reason = "Loop-dispatched phase — in-session sub-agent (agent_runtime=interactive)"

    def claim(self, *, claimed_by: str, claimed_by_session: str = "", lease_seconds: int = 300) -> None:
        _claim_task(self, claimed_by=claimed_by, claimed_by_session=claimed_by_session, lease_seconds=lease_seconds)

    def renew_lease(self, *, lease_seconds: int = 300) -> None:
        _renew_task_lease(self, lease_seconds=lease_seconds)

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
                    "claimed_by_session",
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
        from teatree.core.models.errors import QualityGateError  # noqa: PLC0415 — deferred: ORM/app-registry

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
                    "claimed_by_session",
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
            from teatree.core.models.task_handoff import park_for_user_input  # noqa: PLC0415 — deferred: import cycle

            park_for_user_input(self)
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
        # Normalize once, mirroring _record_phase_visit() — a task whose
        # phase is a short verb ("review"/"code"/...) must advance the
        # FSM too, not just record the session visit (#750). Raw
        # comparison silently desynced ticket.state from visited_phases.
        from teatree.core.modelkit.phases import normalize_phase  # noqa: PLC0415 — deferred: call-time import

        phase = normalize_phase(self.phase)
        # Mirror the FSM source list of mark_reviewed_externally() — guarding
        # only on ``role == REVIEWER`` is not enough (#1000): the #998/#999
        # orphan sweep can complete a second reviewing task on a ticket that
        # already advanced to REVIEW_POSTED (or any other terminal state), and an
        # unconditional FSM call then raises TransitionNotAllowed and crashes
        # the loop tick. Sibling branches below all guard on ``ticket.state``;
        # this branch must too. The source set is DERIVED from the transition
        # declaration (not hand-enumerated) so it can never drift (#808 class).
        mark_reviewed_externally_source_states = transition_source_states("mark_reviewed_externally")
        # The state read + guard + FSM advance all happen inside ONE atomic
        # block with the ticket re-read under ``select_for_update`` (#883/#804
        # discipline). On the production BEGIN IMMEDIATE backend two concurrent
        # completions serialize: the second's re-read sees the first's committed
        # state, its guard no longer matches, and it no-ops — closing the
        # read-then-transition double-fire window (two schedule_* tasks + two
        # Sessions). Reading the state OUTSIDE the atomic (the previous shape)
        # let both completions read the same stale state and both fire.
        with transaction.atomic():
            ticket = Ticket.objects.select_for_update().get(pk=self.ticket_id)  # ty: ignore[unresolved-attribute]
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
                ticket.plan(parent_task=self)
                ticket.save()
            elif phase == "coding" and ticket.state == Ticket.State.PLANNED:
                ticket.code(parent_task=self)
                ticket.save()
            elif (
                phase == "coding"
                and ticket.state in {Ticket.State.NOT_STARTED, Ticket.State.SCOPED, Ticket.State.STARTED}
                and is_auto_implement(ticket)
            ):
                # The issue-implementer auto-start path schedules coding directly
                # on a fresh NOT_STARTED author ticket (no scope/plan phase), so
                # the coding-completion cannot match the PLANNED-source ``code()``
                # guard above. ``code_direct`` is the plan-skipped sibling, gated
                # on the auto-implement marker, so the normal flow is untouched.
                ticket.code_direct(parent_task=self)
                ticket.save()
            elif phase == "testing" and ticket.state == Ticket.State.CODED:
                ticket.test(passed=True, parent_task=self)
                ticket.save()
            elif phase == "reviewing" and ticket.state == Ticket.State.TESTED:
                ticket.review(parent_task=self)
                ticket.save()
                dispose_unshippable_review(ticket)
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
                escalate_unmatched_phase_transition(self, phase=phase, ticket=ticket)
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
        from teatree.core.modelkit.phases import normalize_phase  # noqa: PLC0415 — deferred: call-time import

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

    def fail(self) -> None:
        self.status = self.Status.FAILED
        self._clear_claim()
        self.save(
            update_fields=[
                "status",
                "claimed_at",
                "claimed_by",
                "claimed_by_session",
                "lease_expires_at",
                "heartbeat_at",
            ],
        )

    def reopen(self) -> None:
        if self.status != self.Status.FAILED:
            msg = f"Can only reopen failed tasks, got '{self.status}'"
            raise InvalidTransitionError(msg)
        self.status = self.Status.PENDING
        self.save(update_fields=["status"])

    def park(self, *, not_before: datetime) -> None:
        """Return this task to the queue PENDING, gated until *not_before* (Directive #3).

        The park-not-fail alternative to :meth:`fail`: a usage-window limit is NOT a task
        failure, so the task stays in flight (PENDING) rather than terminal FAILED. The
        claim path skips it while ``not_before`` is in the future, so it does not
        re-dispatch into the same exhausted window; ``usage_window_recovery`` releases it
        once the window re-arms. The park reason is recorded by the caller on the parked
        ``TaskAttempt`` (the ``limit_parked:`` marker), not stored on the task.
        """
        self.status = self.Status.PENDING
        self.not_before = not_before
        self._clear_claim()
        self.save(
            update_fields=[
                "status",
                "not_before",
                "claimed_at",
                "claimed_by",
                "claimed_by_session",
                "lease_expires_at",
                "heartbeat_at",
            ],
        )

    def complete_with_attempt(
        self,
        *,
        artifact_path: str = "",
        exit_code: int = 0,
        error: str = "",
        result: dict[str, object] | None = None,
    ) -> "TaskAttempt":
        task_attempt_model = cast("type[TaskAttempt]", apps.get_model("core", "TaskAttempt"))

        attempt = task_attempt_model.objects.create(
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
        return not children.exclude(status__in=self.Status.terminal()).exists()

    def phase_iteration_count(self) -> int:
        """How many attempts this ticket-phase has already recorded (#2009)."""
        from teatree.core.models.task_repair import phase_attempts  # noqa: PLC0415 — deferred: import cycle

        return len(phase_attempts(self))

    def check_requeue_allowed(self) -> None:
        """Raise if this ticket-phase may NOT be re-queued; escalate on a stall (#2009).

        Delegates to :func:`teatree.core.models.task_repair.check_requeue_allowed`
        (split out for the module-health cap): a phase at the iteration cap raises
        ``MaxIterationsExceeded``; two consecutive identical failure fingerprints
        raise ``IterationStalled`` and record a user-facing ``DeferredQuestion``.
        """
        from teatree.core.models.task_repair import check_requeue_allowed  # noqa: PLC0415 — deferred: import cycle

        check_requeue_allowed(self)

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
                "claimed_by_session",
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
