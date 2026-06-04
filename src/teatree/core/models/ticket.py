import logging
import os
import re
from typing import TYPE_CHECKING, ClassVar

from django.apps import apps
from django.db import models, transaction
from django.utils import timezone
from django_fsm import FSMField, TransitionNotAllowed, transition

from teatree.config import Mode, get_effective_settings, load_config
from teatree.core.managers import TicketManager
from teatree.core.models.errors import DirtyWorktreeError, InvalidTransitionError
from teatree.core.models.types import validated_ticket_extra
from teatree.utils import git, redis_container
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)


def _check_plan_artifact(ticket: object) -> bool:
    from teatree.core.plan_gate import check_plan_artifact  # noqa: PLC0415

    return check_plan_artifact(ticket)  # type: ignore[arg-type]


def _auto_ship_enabled() -> bool:
    if os.environ.get("T3_AUTO_SHIP", "").lower() == "true":
        return True
    return get_effective_settings().mode == Mode.AUTO


if TYPE_CHECKING:
    from teatree.core.models.session import Session
    from teatree.core.models.task import Task
    from teatree.core.models.types import ReviewContext, ReviewSkillRun, TicketExtra, TicketSiblingFields
    from teatree.core.models.worktree import Worktree


class Ticket(models.Model):  # noqa: PLR0904 — FSM transition surface; method count reflects the lifecycle state graph, not poor encapsulation.
    class State(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        SCOPED = "scoped", "Scoped"
        STARTED = "started", "Started"
        PLANNED = "planned", "Planned"
        CODED = "coded", "Coded"
        TESTED = "tested", "Tested"
        REVIEWED = "reviewed", "Reviewed"
        SHIPPED = "shipped", "Shipped"
        IN_REVIEW = "in_review", "In review"
        MERGED = "merged", "Merged"
        RETROSPECTED = "retrospected", "Retrospected"
        DELIVERED = "delivered", "Delivered"
        IGNORED = "ignored", "Ignored"

    class Role(models.TextChoices):
        AUTHOR = "author", "Author"
        REVIEWER = "reviewer", "Reviewer"

    class Kind(models.TextChoices):
        FEATURE = "feature", "Feature"
        FIX = "fix", "Fix"

    # #808: the ship reconcile is PHASE-DRIVEN / state-complete, not an
    # enumerated source allow-list. The shipping gate already verified the
    # aggregated cross-session phase ledger (the single source of truth)
    # before calling ``reconcile_reviewed``; the FSM must follow the
    # phases, not gate them behind a hand-maintained state list (the
    # recurring #798/#799/#808 ``{'allowed': False, 'missing': []}``
    # class — each new unlisted non-terminal state re-broke it). Only
    # genuinely terminal/abandoned states are non-recoverable; EVERY other
    # state is a legal reconcile source, derived (not enumerated) so a
    # future added state cannot silently re-introduce the bug.
    _TERMINAL_STATES: ClassVar[frozenset[str]] = frozenset(
        {State.SHIPPED, State.MERGED, State.DELIVERED, State.IGNORED},
    )
    # NOTE: a class-body comprehension cannot see the enclosing ``State``
    # (Python scoping); enumerate explicitly and assert completeness in a
    # test so a future added state is caught rather than silently dropped.
    _RECONCILE_SOURCE_STATES: ClassVar[list[str]] = [
        State.NOT_STARTED,
        State.SCOPED,
        State.STARTED,
        State.PLANNED,
        State.CODED,
        State.TESTED,
        State.REVIEWED,
        State.IN_REVIEW,
        State.RETROSPECTED,
    ]
    # #1343: PR-merge reconcile catches every PRE-MERGED state. The
    # original guard only fired ``mark_merged()`` from IN_REVIEW/MERGED,
    # so tickets whose PR landed while the FSM still read STARTED stayed
    # stuck on the statusline. The merge keystone calls
    # ``reconcile_merged()``, which targets MERGED from every pre-merged
    # state (and is idempotent at MERGED). RETROSPECTED/DELIVERED are
    # past MERGED and must not be dragged backward; IGNORED is abandoned.
    _MERGED_RECONCILE_SOURCE_STATES: ClassVar[list[str]] = [
        State.NOT_STARTED,
        State.SCOPED,
        State.STARTED,
        State.PLANNED,
        State.CODED,
        State.TESTED,
        State.REVIEWED,
        State.SHIPPED,
        State.IN_REVIEW,
        State.MERGED,
    ]

    overlay = models.CharField(max_length=255)
    issue_url = models.URLField(max_length=500, blank=True)
    variant = models.CharField(max_length=100, blank=True)
    repos = models.JSONField(default=list, blank=True)
    state = FSMField(max_length=32, choices=State.choices, default=State.NOT_STARTED)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.AUTHOR)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.FEATURE)
    extra = models.JSONField(default=dict, blank=True)
    context = models.TextField(blank=True, default="")
    short_description = models.CharField(max_length=80, blank=True, default="")
    redis_db_index = models.IntegerField(null=True, blank=True, unique=True)

    objects = TicketManager()

    class Meta:
        db_table = "teatree_ticket"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["issue_url"],
                name="unique_nonempty_issue_url",
                condition=~models.Q(issue_url=""),
            ),
        ]

    def __str__(self) -> str:
        return str(self.issue_url or f"ticket-{self.pk}")

    def save(self, *args: object, **kwargs: object) -> None:
        if not self.overlay and self.issue_url:
            self.overlay = self._infer_overlay()
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def _infer_overlay(self) -> str:
        """Derive overlay name from ``issue_url`` (see ``infer_overlay_for_url``)."""
        from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415

        return infer_overlay_for_url(self.issue_url)

    def apply_inferred_overlay(self, inferred: str) -> bool:
        """Persist ``inferred`` as the overlay when it is a conclusive change.

        Returns ``True`` when the row's overlay was changed. An inconclusive
        (empty) inference leaves the existing value untouched — a blank result
        must never blank out a manually-set or previously-correct attribution.
        Callers that already computed the inference reuse it here rather than
        re-walking the overlay registry.
        """
        if not inferred or inferred == self.overlay:
            return False
        self.overlay = inferred
        Ticket.objects.filter(pk=self.pk).update(overlay=inferred)
        return True

    def reconcile_overlay(self) -> bool:
        """Re-infer ``overlay`` from ``issue_url`` and persist a correction."""
        return self.apply_inferred_overlay(self._infer_overlay())

    @property
    def is_terminal(self) -> bool:
        """True when the ticket is in a genuinely terminal/abandoned state.

        The public read of the model-owned terminal set (SHIPPED/MERGED/
        DELIVERED/IGNORED): a terminal ticket is past recovery, so the outage
        recovery sweep (#1764) skips its FAILED tasks rather than re-queuing
        work that has already shipped.
        """
        return self.state in self._TERMINAL_STATES

    @property
    def ticket_number(self) -> str:
        match = re.search(r"(\d+)$", self.issue_url)
        if match and match.group(1) != "0":
            return match.group(1)
        return str(self.pk)

    @transition(field=state, source=State.NOT_STARTED, target=State.SCOPED)
    def scope(
        self,
        *,
        issue_url: str | None = None,
        variant: str | None = None,
        repos: list[str] | None = None,
    ) -> None:
        if issue_url is not None:
            self.issue_url = issue_url
        if variant is not None:
            self.variant = variant
        if repos is not None:
            self.repos = repos

    @transition(field=state, source=[State.SCOPED, State.STARTED], target=State.STARTED)
    def start(self) -> None:
        """Schedule worktree provisioning + planning task.

        The worker creates per-repo git worktrees, then calls
        ``schedule_planning()`` once the layout exists. FSM invariant (BLUEPRINT
        §4): transition bodies stay pure — long I/O is offloaded to an
        ``@task`` worker, enqueued after commit so the state change and the
        queued work land atomically.

        Source ``[SCOPED, STARTED]`` makes re-firing idempotent: if the previous
        provisioning worker failed, the operator can re-call ``start()``
        without rolling back through ``rework``. The worker's own state guard
        prevents duplicate work when provisioning already succeeded.
        """
        from teatree.core.tasks import execute_provision  # noqa: PLC0415

        ticket_pk = int(self.pk)
        transaction.on_commit(lambda: execute_provision.enqueue(ticket_pk))

    @transition(
        field=state,
        source=State.STARTED,
        target=State.PLANNED,
        conditions=[_check_plan_artifact],
    )
    def plan(self) -> None:
        """Advance STARTED → PLANNED after a PlanArtifact record exists.

        Guarded by check_plan_artifact() — requires at least one PlanArtifact
        row for this ticket.  The condition is the single source of truth for
        the plan gate; no prose rule or wall-clock check is needed.
        """
        self._consume_pending_phase_tasks("planning")

    @transition(field=state, source=State.PLANNED, target=State.CODED)
    def code(self) -> None:
        self._refuse_if_worktree_dirty("coding")
        self._consume_pending_phase_tasks("coding")
        self.schedule_testing()

    @transition(field=state, source=State.CODED, target=State.TESTED)
    def test(self, *, passed: bool = True) -> None:
        self._refuse_if_worktree_dirty("testing")
        extra = self._extra()
        extra["tests_passed"] = passed
        self.extra = extra
        self._consume_pending_phase_tasks("testing")
        self.schedule_review()

    @transition(
        field=state,
        source=State.TESTED,
        target=State.REVIEWED,
        conditions=[
            lambda t: t.tasks.completed_in_phase("reviewing").exists(),
            lambda t: t.review_context_satisfied(),
        ],
    )
    def review(self) -> None:
        self._refuse_if_worktree_dirty("reviewing")
        self._consume_pending_phase_tasks("reviewing")
        if self.has_shippable_diff():
            self.schedule_shipping()
            return
        logger.info(
            "Ticket %s reviewed with no shippable diff; skipping auto-shipping (likely meta or already-shipped work)",
            self.pk,
        )
        extra = self._extra()
        extra["shipping_skipped"] = "no shippable diff — likely meta or already-shipped work"
        self.extra = extra

    @transition(
        field=state,
        source=_RECONCILE_SOURCE_STATES,
        target=State.REVIEWED,
    )
    def reconcile_reviewed(self) -> None:
        """Phase-driven, state-complete FSM catch-up to REVIEWED (#694, #798, #799, #808).

        EVERY non-terminal state reconciles to ``REVIEWED`` — the source
        set is *derived* from ``_RECONCILE_SOURCE_STATES`` (all states
        except the terminal ``_TERMINAL_STATES``: SHIPPED/MERGED/DELIVERED/
        IGNORED), never a hand-maintained allow-list.

        The shipping gate is the single source of truth: it verifies the
        required phases aggregated across **all** of the ticket's sessions
        (``aggregate_phase_records``/``check_gate_across_ticket``) *before*
        calling this. Unlike ``review()``, there is no completed-reviewing-
        task condition — the session record already attests the work was
        done. So a passing gate must imply a shippable FSM state and
        ``ship()`` never raises a raw ``TransitionNotAllowed`` at
        ``pr create``.

        #808 made this state-complete: previously the source was an
        enumerated list (#799 added ``IN_REVIEW`` after #798; ``RETROSPECTED``
        and any future unlisted non-terminal state was still rejected),
        which kept re-introducing the ``{'allowed': False, 'missing': []}``
        denial — the gate aggregated ``missing: []`` but the FSM couldn't
        reach ``REVIEWED`` from the lingering state (e.g. a ticket
        re-provisioned for a new workstream whose FSM sat at
        ``RETROSPECTED``). Deriving the source from the terminal set makes
        the FSM follow the phase ledger, so a newly added non-terminal
        state can never silently re-break the gate. Terminal states stay
        non-recoverable: SHIPPED/MERGED/DELIVERED are genuine post-ship
        success; IGNORED is abandoned — none should reconcile backward to a
        shippable state.

        This transition body stays pure: task ledger consumption is the
        caller's responsibility on the gate-verified path
        (``reconcile_fsm_for_ship``). Calling this directly from the
        ungated ``ticket transition`` CLI or from ``--skip-validation``
        must NOT complete active reviewing tasks — those paths skip the
        attestation that would justify it.
        """

    def aggregate_phase_records(self) -> tuple[list[str], dict[str, dict[str, str]]]:
        """Union the phase records across all of this ticket's sessions (#694).

        Returns ``(visited_phases, phase_visits)`` merged across
        ``self.sessions`` in creation order. ``visited_phases`` is a
        de-duplicated list; ``phase_visits`` keeps the first recorded
        ``agent_id`` per phase (earliest session wins) as a deterministic
        audit trail of who recorded each phase — it is not consumed for
        gate enforcement. The shipping gate consumes the ``visited_phases``
        union because FSM-advancing ``visit-phase`` forks fresh sessions by
        design — the required phases are legitimately scattered, and the
        single source of truth is the ticket's lifecycle, not one session.
        """
        visited: list[str] = []
        visits: dict[str, dict[str, str]] = {}
        for session in self.sessions.order_by("pk"):  # ty: ignore[unresolved-attribute]
            for phase in session.visited_phases or []:
                if phase not in visited:
                    visited.append(phase)
            for phase, record in (session.phase_visits or {}).items():
                if phase not in visits:
                    visits[phase] = record
        return visited, visits

    def resolve_phase_session(self, *, agent_id: str = "loop") -> "Session":
        """The single canonical phase-visit session for the attestation writers (#801).

        Which ``Session`` a phase visit lands on was decided four
        inconsistent ways (``ensure_session`` earliest+locked; the
        ``lifecycle visit-phase`` CLI, the ``tasks`` phase-handoff
        command each ``order_by("-pk")`` *latest* with an unlocked raw
        blank-``agent_id`` create on miss; the ``pr`` gate *latest* as
        its gate object). A CLI visit then wrote the *latest* session
        while dispatch reused the *earliest*, splitting attestation
        across sessions (#801). The three attestation writers now route
        here; the read-only gate uses :meth:`find_phase_session`.

        Policy: the **earliest** session (``order_by("pk")`` — the one
        dispatch's attestation uses, so the ledger never splits),
        selected/created inside one ``transaction.atomic()`` with the
        ticket row ``select_for_update``-locked (dispatch callers have
        no surrounding transaction, so concurrent loop ticks for the
        same ``issue_url`` must serialise). Always returns a Session —
        on miss it creates one with a guaranteed **non-blank**
        ``agent_id`` (never the raw blank-``agent_id`` create that left
        the ``phase_visits`` audit trail unattributed).
        """
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        with transaction.atomic():
            Ticket.objects.select_for_update().filter(pk=self.pk).first()
            existing = self.sessions.order_by("pk").first()  # ty: ignore[unresolved-attribute]
            if existing is not None:
                return existing
            return Session.objects.create(ticket=self, agent_id=agent_id.strip() or "loop")

    def find_phase_session(self) -> "Session | None":
        """Read-only canonical phase-visit session for the gate (#801).

        Same earliest + ticket-row-locked selection policy as
        :meth:`resolve_phase_session` but **never creates** — a gate
        check must not have the side effect of minting a session.
        """
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        with transaction.atomic():
            Ticket.objects.select_for_update().filter(pk=self.pk).first()
            return self.sessions.order_by("pk").first()  # ty: ignore[unresolved-attribute]

    def ensure_session(self, *, agent_id: str = "loop") -> "Session":
        """Durable phase-attestation Session for this ticket (#748).

        Thin alias of the canonical :meth:`resolve_phase_session` (#801
        SSOT) — kept for its existing callers / API.
        """
        return self.resolve_phase_session(agent_id=agent_id)

    def has_shippable_diff(self) -> bool:
        """Return True iff at least one worktree has commits ahead of its base branch.

        Used by ``review()`` to skip auto-scheduling shipping when there is
        nothing to ship — typically meta-tracker tickets whose work already
        landed via sibling PRs. Manual ``schedule_shipping()`` callers are not
        gated.
        """
        worktree_model = apps.get_model("core", "Worktree")
        return any(_worktree_has_commits_ahead(wt) for wt in worktree_model.objects.filter(ticket=self))

    def schedule_planning(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless planning task after provisioning completes."""
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

        if self.role != self.Role.AUTHOR:
            msg = f"schedule_planning requires role=author (got role={self.role!r})"
            raise InvalidTransitionError(msg)
        session = Session.objects.create(ticket=self, agent_id="planning")
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="planning",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-scheduled planning — produce a plan before coding",
            parent_task=parent_task,
        )

    def schedule_coding(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless coding task after planning completes."""
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

        if self.role != self.Role.AUTHOR:
            msg = f"schedule_coding requires role=author (got role={self.role!r})"
            raise InvalidTransitionError(msg)
        session = Session.objects.create(ticket=self, agent_id="coding")
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-scheduled coding — implement the ticket",
            parent_task=parent_task,
        )

    def schedule_testing(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless testing task after coding completes."""
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

        session = Session.objects.create(ticket=self, agent_id="testing")
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="testing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-scheduled testing — run + QA the coding work",
            parent_task=parent_task,
        )

    def schedule_review(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless review+retro task (new session for bias-free evaluation)."""
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

        session = Session.objects.create(ticket=self, agent_id="review")
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-scheduled review + retro — fresh agent, no bias",
            parent_task=parent_task,
        )

    def schedule_review_in_session(self, session: "Session", *, parent_task: "Task | None" = None) -> "Task":
        """Create a review task within an existing session (sub-agent, not a new session)."""
        from teatree.core.models.task import Task  # noqa: PLC0415

        return Task.objects.create(
            ticket=self,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-review before shipping — sub-agent in current session",
            parent_task=parent_task,
        )

    def schedule_shipping(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create an INTERACTIVE shipping task; approval gating rides the reason.

        Shipping is a loop-dispatched phase (``(author, shipping)`` →
        ``t3:shipper``), so it runs as an in-session sub-agent
        (subscription-covered), never a metered ``claude -p`` — regardless of
        auto mode. Auto mode no longer changes the execution *target*; it only
        changes the *approval posture* the in-session shipper reads from
        ``execution_reason`` (auto = push without waiting; otherwise = gate for
        user approval first).
        """
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

        session = Session.objects.create(ticket=self, agent_id="shipping")
        if _auto_ship_enabled():
            reason = "Auto-scheduled shipping — auto mode, push will proceed without waiting for approval"
        else:
            reason = (
                "Auto-scheduled shipping — gated for user approval "
                '(set teatree.mode = "auto" or T3_AUTO_SHIP=true to skip)'
            )
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="shipping",
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            execution_reason=reason,
            parent_task=parent_task,
        )

    @transition(
        field=state,
        source=[
            State.NOT_STARTED,
            State.SCOPED,
            State.STARTED,
            State.PLANNED,
            State.CODED,
            State.TESTED,
            State.REVIEWED,
        ],
        target=State.DELIVERED,
        conditions=[
            lambda t: t.role == Ticket.Role.REVIEWER and t.tasks.completed_in_phase("reviewing").exists(),
            lambda t: t.review_context_satisfied(),
        ],
    )
    def mark_reviewed_externally(self) -> None:
        """Reviewer-role short-circuit: any pre-shipped state → DELIVERED.

        External review tickets bypass the implementation lifecycle. Once
        the reviewing task completes, the ticket is done — the reviewer has
        posted their review on someone else's PR. We also stamp the head
        SHA + ``last_review_state`` on ``extra`` so ``ReviewerPrsScanner``
        won't re-spawn the reviewer agent for the same PR until either the
        SHA moves or the forge dismisses the approval.
        """
        from teatree.backends.protocols import ReviewState  # noqa: PLC0415

        sha = str(self._extra().get("reviewed_sha", ""))
        if self.issue_url and sha:
            # #800 N3: canonical locked RMW — a concurrent pr_urls /
            # visual_qa writer no longer clobbers reviewed_sha /
            # last_review_state.
            self.merge_extra(set_keys={"reviewed_sha": sha, "last_review_state": ReviewState.APPROVED.value})

    @transition(
        field=state,
        source=[
            State.NOT_STARTED,
            State.SCOPED,
            State.STARTED,
            State.PLANNED,
            State.CODED,
            State.TESTED,
            State.REVIEWED,
        ],
        target=State.DELIVERED,
        conditions=[lambda t: t.role == Ticket.Role.REVIEWER],
    )
    def mark_review_no_action(self) -> None:
        """Reviewer-role terminal disposition for a no-postable-action review.

        Sibling of :meth:`mark_reviewed_externally` for the case the
        reviewer concludes an external review with nothing to post or
        approve (e.g. a bot MR — Aikido/Dependabot — where there is no
        diff worth commenting on and no approval to give). The reviewing
        Task would otherwise never reach a terminal state — the only
        terminal path is ``Task.complete()`` → ``mark_reviewed_externally``
        which requires an APPROVED outcome — so ``pending-spawn``
        re-dispatched the same task every Stop-hook pump forever (#1077).

        Unlike ``mark_reviewed_externally`` (fired *from* an
        already-COMPLETED task) this transition is driven directly via
        ``t3 ticket transition <id> mark_review_no_action`` while the
        reviewing task is still PENDING, so it consumes that task itself.
        It records ``last_review_state = REVIEWED_NO_ACTION`` (NEVER
        APPROVED): the dedup's APPROVED-only suppression therefore does not
        hide a future *genuine* review, while ``_already_reviewed_at_head``
        still treats a no-action observation at the current head SHA as
        "already handled" so the task is not re-queued. A head-SHA move
        drops ``last_review_state`` (the existing #959 reset) so a new
        revision is still reviewed — no lost obligation.
        """
        from teatree.backends.protocols import ReviewState  # noqa: PLC0415

        sha = str(self._extra().get("reviewed_sha", ""))
        if self.issue_url and sha:
            self.merge_extra(set_keys={"reviewed_sha": sha, "last_review_state": ReviewState.REVIEWED_NO_ACTION.value})
        self._consume_pending_phase_tasks("reviewing")

    @transition(field=state, source=[State.REVIEWED, State.SHIPPED], target=State.SHIPPED)
    def ship(self) -> None:
        """Schedule push + PR creation.

        The worker pushes the worktree branch, opens the pull request, and
        calls ``request_review()`` on success. FSM invariant (BLUEPRINT §4):
        transition bodies stay pure — long I/O is offloaded to an ``@task``
        worker, enqueued after commit so the state change and the queued work
        land atomically.

        Source ``[REVIEWED, SHIPPED]`` makes re-firing idempotent: if the
        previous ship worker failed (push rejected, code host unavailable,
        credentials missing), the operator can re-call ``ship()`` to retry.
        The worker's own state guard skips duplicate work if push already
        succeeded.

        Two preflight guards run before any scheduling side effect, mirroring
        each other: ``_refuse_if_worktree_dirty`` (#884) and the #88 DoD gate
        (``check_local_e2e_dod`` — a UI-visible ticket must have a green
        local-stack E2E artifact, or an explicit recorded override). Both
        raise a :class:`InvalidTransitionError` subclass so the loop's outer
        atomic rolls the advance back and the FSM stays put.
        """
        from teatree.core.dod_gate import check_local_e2e_dod  # noqa: PLC0415
        from teatree.core.tasks import execute_ship  # noqa: PLC0415

        self._refuse_if_worktree_dirty("shipping")
        check_local_e2e_dod(self)
        self._consume_pending_phase_tasks("shipping")
        ticket_pk = int(self.pk)
        transaction.on_commit(lambda: execute_ship.enqueue(ticket_pk))

    @transition(field=state, source=State.SHIPPED, target=State.IN_REVIEW)
    def request_review(self) -> None:
        pass

    @transition(field=state, source=[State.IN_REVIEW, State.MERGED], target=State.MERGED)
    def mark_merged(self) -> None:
        """Schedule worktree teardown.

        The worker removes git worktrees, deletes the local branch, drops the
        per-worktree DB and runs overlay cleanup hooks. FSM invariant
        (BLUEPRINT §4): transition bodies stay pure — long I/O is offloaded
        to an ``@task`` worker, enqueued after commit so the state change and
        the queued work land atomically.

        Source ``[IN_REVIEW, MERGED]`` makes re-firing idempotent: if a
        previous teardown reported errors, the operator can re-call
        ``mark_merged()`` to retry. The worker is best-effort and does not
        advance the FSM, so retries are safe.
        """
        from teatree.core.tasks import execute_teardown  # noqa: PLC0415

        ticket_pk = int(self.pk)
        transaction.on_commit(lambda: execute_teardown.enqueue(ticket_pk))

    @transition(
        field=state,
        source=_MERGED_RECONCILE_SOURCE_STATES,
        target=State.MERGED,
    )
    def reconcile_merged(self) -> None:
        """State-complete FSM catch-up to ``MERGED`` on PR-merge (#1343).

        The merge keystone (``merge_execution.record_merge_and_advance``)
        calls this from its post hook: an authorised, audited PR-merge is
        the authority — whatever pre-merged state the ticket sat in, the
        FSM must follow. Mirrors ``reconcile_reviewed`` (#808) — the source
        is derived from the pre-merged set so a future-added pre-merged
        state cannot silently re-introduce the stale-``started`` class.

        Post-MERGED states (``RETROSPECTED``/``DELIVERED``) and ``IGNORED``
        are NOT sources: the keystone must never drag a ticket BACKWARD
        from a state past MERGED.

        Schedules the same teardown work as ``mark_merged`` so a
        keystone-driven reconcile cleans up worktrees identically to the
        normal IN_REVIEW → MERGED path.
        """
        from teatree.core.tasks import execute_teardown  # noqa: PLC0415

        ticket_pk = int(self.pk)
        transaction.on_commit(lambda: execute_teardown.enqueue(ticket_pk))

    @transition(field=state, source=[State.MERGED, State.RETROSPECTED], target=State.RETROSPECTED)
    def retrospect(self) -> None:
        """Schedule retrospection I/O.

        The worker writes retro artifacts and calls ``mark_delivered()`` on
        success. FSM invariant (BLUEPRINT §4): transition bodies stay pure —
        long I/O is offloaded to an ``@task`` worker, enqueued after commit so
        the state change and the queued work land atomically.

        Source ``[MERGED, RETROSPECTED]`` makes re-firing idempotent: if a
        previous retro worker failed, the operator can re-call ``retrospect()``
        to retry. The worker's own state guard skips when retrospection
        already produced its artifacts.
        """
        from teatree.core.tasks import execute_retrospect  # noqa: PLC0415

        ticket_pk = int(self.pk)
        transaction.on_commit(lambda: execute_retrospect.enqueue(ticket_pk))

    @transition(field=state, source=State.RETROSPECTED, target=State.DELIVERED)
    def mark_delivered(self) -> None:
        """Reach DELIVERED (done).

        For a ``kind=fix`` ticket the Definition of Done requires a validated
        FixRecord: ``check_fix_record_dod`` raises :class:`FixRecordDodError`
        (an ``InvalidTransitionError`` subclass) so the loop's outer atomic
        rolls the advance back and the ticket stays RETROSPECTED — merged on
        the forge, but not yet *done*. A manifestation patch with no stated
        root cause cannot reach DELIVERED. Feature tickets pass unconditionally.
        """
        from teatree.core.fix_dod_gate import check_fix_record_dod  # noqa: PLC0415

        check_fix_record_dod(self)

    @transition(field=state, source=[State.CODED, State.TESTED, State.REVIEWED], target=State.STARTED)
    def rework(self) -> None:
        extra = self._extra()
        extra.pop("tests_passed", None)
        self.extra = extra
        self._cancel_pending_tasks()

    @transition(
        field=state,
        source=[State.SHIPPED, State.IN_REVIEW, State.MERGED, State.RETROSPECTED],
        target=State.STARTED,
    )
    def reopen(self) -> None:
        """Reopen a post-ship ticket back to STARTED.

        Triggered when new draft MRs appear after the ticket was shipped,
        indicating additional work is needed.

        #1286: retire every session's phase ledger here — ``reopen()`` is
        the explicit workstream-boundary transition. Without this, the
        prior workstream's ``testing``/``reviewing`` attestations remain
        in ``aggregate_phase_records()`` and false-pass the next
        workstream's shipping gate (the ``AGENTS.md`` § "Reused-ticket
        attestation" risk). Same operation the sanctioned
        ``lifecycle clear-ledger --confirm`` performs, run inside the FSM
        transition body so the cross-workstream gate-bypass is structurally
        foreclosed rather than relying on the agent remembering to call
        ``clear-ledger`` on reuse.
        """
        extra = self._extra()
        extra.pop("tests_passed", None)
        extra["reopened_from"] = self.state
        self.extra = extra
        self._cancel_pending_tasks()
        self._retire_phase_ledger()

    @transition(
        field=state,
        source=[
            State.NOT_STARTED,
            State.SCOPED,
            State.STARTED,
            State.PLANNED,
            State.CODED,
            State.TESTED,
            State.REVIEWED,
            State.SHIPPED,
            State.IN_REVIEW,
            State.MERGED,
            State.RETROSPECTED,
        ],
        target=State.IGNORED,
    )
    def ignore(self) -> None:
        extra = self._extra()
        extra["ignored_from"] = self.state
        self.extra = extra

    def unignore(self) -> None:
        if self.state != self.State.IGNORED:
            msg = f"Can't unignore from state '{self.state}'"
            raise TransitionNotAllowed(msg)
        extra = self._extra()
        previous = extra.pop("ignored_from", self.State.NOT_STARTED)
        self.extra = extra
        self.state = str(previous)

    def release_redis_slot(self) -> None:
        """FLUSHDB on the ticket's Redis DB index and clear the field."""
        if self.redis_db_index is None:
            return
        index = self.redis_db_index
        redis_container.flushdb(index, db_count=load_config().user.redis_db_count)
        self.redis_db_index = None
        self.save(update_fields=["redis_db_index"])

    def _cancel_pending_tasks(self) -> None:
        """Fail all pending/claimed tasks when reworking."""
        from teatree.core.models.task import Task  # noqa: PLC0415

        for task in self.tasks.filter(status__in=[Task.Status.PENDING, Task.Status.CLAIMED]):  # type: ignore[attr-defined]  # Django reverse FK
            task.fail()

    def _retire_phase_ledger(self) -> None:
        """Retire every session's phase ledger for this ticket (#1286).

        Mirrors ``lifecycle clear-ledger --confirm``: a per-session reset
        of ``visited_phases``, ``phase_visits``, ``repos_modified``,
        ``repos_tested`` so the next workstream re-earns its attestations
        from scratch. Invoked from ``reopen()`` because that transition
        IS the workstream boundary — prior testing/reviewing no longer
        attest the new work.

        Wrapped in ``transaction.atomic`` so the ``select_for_update``
        works even when the FSM caller has not opened a surrounding
        transaction (the loop ``reopen_ticket`` mechanical path).
        """
        with transaction.atomic():
            for session in self.sessions.select_for_update().all():  # type: ignore[attr-defined]  # Django reverse FK
                session.visited_phases = []
                session.phase_visits = {}
                session.repos_modified = []
                session.repos_tested = []
                session.save(
                    update_fields=["visited_phases", "phase_visits", "repos_modified", "repos_tested"],
                )

    def _refuse_if_worktree_dirty(self, phase: str) -> None:
        """Preflight gate (#884): refuse the transition if a worktree is tracked-dirty.

        Run at the top of the ``code``/``test``/``review``/``ship``
        transition bodies, before any scheduling side effect. Owner-resolved
        policy: a worktree with uncommitted *tracked* changes must not
        advance the FSM — the agent has to commit or discard first. We do
        NOT auto-stash: teatree worktrees share one ``.git`` so a stash is
        repo-global and would clobber an unrelated branch's work (the
        foreign-stash hazard, near-miss class #806).

        Untracked-only files do not block (the #925 distinction, mirroring
        ``cli.update._tracked_dirty_paths``): a fast-forward never conflicts
        with untracked scratch, and the loop legitimately leaves scratch
        files around. Only a tracked modification — work the agent forgot to
        commit — is the refusal trigger.

        On dirty: a loud :class:`DirtyWorktreeError` is raised naming the
        dirty worktree. The transition does **not** advance — every
        production caller wraps the transition body in an *outer*
        ``transaction.atomic`` (the loop: ``Task.complete()`` →
        ``_advance_ticket`` → ``_apply_phase_transition``; ship:
        ``_ship_exec._do_ship_transition``), so the raise rolls that whole
        atomic back and the ticket stays put. **The task is not
        force-reopened here** — there is no cross-transaction durable write
        that could survive the caller's rollback, so attempting one only
        adds a false durability claim. Held-task recovery is the existing
        **lease-reaper safety net**: the worker that called the transition
        stops heartbeating after the exception, the task's lease expires,
        and ``TaskManager.reclaim_orphaned_claims`` returns the CLAIMED task
        to PENDING on the next loop tick so the agent re-runs it and
        finishes the commit. Mirrors the existing loud-refusal convention
        (``InvalidTransitionError`` in ``schedule_coding`` / the #694 gate).
        """
        worktree_model = apps.get_model("core", "Worktree")
        dirty = [
            path
            for wt in worktree_model.objects.filter(ticket=self)
            if (path := _worktree_tracked_dirty_path(wt)) is not None
        ]
        if not dirty:
            return
        joined = ", ".join(dirty)
        msg = (
            f"Refusing the '{phase}' transition for ticket {self} — uncommitted tracked "
            f"changes in worktree(s): {joined}. Commit or discard them, then retry. "
            f"(No auto-stash: teatree worktrees share one .git, so a stash is repo-global "
            f"and could clobber another branch — #806.)"
        )
        raise DirtyWorktreeError(msg)

    def _consume_pending_phase_tasks(self, phase: str) -> None:
        """Mark non-terminal tasks for ``phase`` as COMPLETED.

        FSM transitions advance ticket state via two paths: the task-driven
        chain (``Task.complete()`` → ``_advance_ticket()`` → transition body),
        and direct CLI/API calls (e.g. ``pr.py`` calling ``ticket.ship()``).
        On the task-driven path the task is already COMPLETED before this runs
        — the filter is empty and this is a no-op. On the direct path the
        previously-scheduled phase task is orphaned in PENDING/CLAIMED and
        would be picked up later as a zombie session; consume it now.

        Matches any accepted phase spelling via ``pending_in_phase`` (#769,
        the consume-side mirror of #757's ``completed_in_phase``): a raw
        ``phase=phase`` filter missed a short-verb ``review`` task stored
        by the unnormalized ``tasks create <id> review`` path, leaving it
        as a zombie session.
        """
        from teatree.core.models.task import Task  # noqa: PLC0415

        Task.objects.pending_in_phase(phase).filter(ticket=self).update(
            status=Task.Status.COMPLETED,
            claimed_at=None,
            claimed_by="",
            lease_expires_at=None,
            heartbeat_at=None,
        )

    def _extra(self) -> "TicketExtra":
        return validated_ticket_extra(self.extra)

    def merge_extra(
        self,
        *,
        set_keys: "TicketExtra | None" = None,
        pop_keys: "list[str] | None" = None,
        also_set: "TicketSiblingFields | None" = None,
    ) -> None:
        """Canonical locked read-modify-write of ``extra`` (#800 N3).

        Several writers mutate shared ``extra`` JSON — ``pr_urls`` (ship
        worker), ``visual_qa`` (the pre-push gate), ``reviewed_sha`` /
        ``last_review_state`` (reviewer path). Done as an unlocked
        ``self.extra = …; self.save(update_fields=["extra"])`` they
        last-writer-clobber each other's key (the Haki-Benita
        lost-update). This is the single primitive every ``extra``
        mutation routes through, with the same shape as
        ``Session.visit_phase``: the RMW runs in ``transaction.atomic()``
        with the row ``select_for_update``-locked and **re-read from the
        locked row** (not the possibly-stale in-memory instance), so a
        concurrent writer's key survives the merge instead of being
        overwritten. The locked re-read is what makes it correct on the
        production SQLite backend (where ``select_for_update`` is a no-op
        but the #804 ``BEGIN IMMEDIATE`` serialises the writers, so the
        re-read sees the other writer's committed key).

        ``also_set`` writes sibling **model fields** (``state``,
        ``repos``, ``variant``, …) in the SAME locked ``UPDATE`` as
        ``extra``. The tracker-sync paths legitimately co-write
        ``extra`` with ``state``/``repos`` in one ``save`` — routing
        them through here keeps that write atomic (no split into two
        non-atomic writes) while still going through the single locked
        primitive, so the SSOT holds with zero unlocked ``extra`` RMW
        anywhere.
        """
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            merged = dict(locked.extra or {})
            if set_keys:
                merged.update(set_keys)
            for key in pop_keys or []:
                merged.pop(key, None)
            self.extra = merged
            for field, value in (also_set or {}).items():
                setattr(self, field, value)
            type(self).objects.filter(pk=self.pk).update(extra=merged, **(also_set or {}))

    def record_review_skill_run(self, skill: str) -> None:
        """Stamp durable evidence that the deep-review ``skill`` ran (#1539).

        Written through the canonical locked ``merge_extra`` primitive so a
        concurrent ``extra`` writer's key survives. The timestamp is UTC ISO
        so the reviewing-phase gate's audit trail is timezone-unambiguous.
        """
        run: ReviewSkillRun = {"skill": skill, "at": timezone.now().isoformat()}
        self.merge_extra(set_keys={"review_skill_run": run})

    def record_review_context(self, work_item: str, documents: list[str], analysis: str) -> None:
        """Stamp durable evidence the referenced context was retrieved + analyzed.

        Reviewing carries the same responsibility as implementing: the
        ``-> reviewing`` deep-retrieval gate (``teatree.core.review_context_gate``)
        reads this to refuse a verdict formed from the diff alone. ``work_item``
        is the fetched ticket / work-item source, ``documents`` the downloaded
        references, ``analysis`` how the implementation was checked against the
        specified requirements. Written through the canonical locked
        ``merge_extra`` primitive so a concurrent ``extra`` writer's key
        survives; the timestamp is UTC ISO.
        """
        context: ReviewContext = {
            "work_item": work_item,
            "documents": list(documents),
            "analysis": analysis,
            "at": timezone.now().isoformat(),
        }
        self.merge_extra(set_keys={"review_context": context})

    def review_context_satisfied(self) -> bool:
        """Whether the ``-> reviewing`` deep-retrieval precondition is met.

        An FSM ``condition`` on ``review()``: the ``TESTED -> REVIEWED``
        transition is mechanically refused (``TransitionNotAllowed``) when
        ``require_review_context`` is on and no complete ``review_context``
        artifact is recorded — so a verdict from the diff alone cannot advance
        the FSM regardless of entry path. NO-OP (returns ``True``) when the knob
        is off (opt-in default preserved).
        """
        from teatree.core.review_context_gate import (  # noqa: PLC0415
            is_complete,
            recorded_review_context,
            review_context_required,
        )

        if not review_context_required():
            return True
        return is_complete(recorded_review_context(self))

    def append_context(self, entry: str) -> str:
        r"""Append a timestamped block to the durable per-ticket knowledge store (#627).

        ``context`` is append-only: parallel sessions on the same ticket each
        add their own ``\n\n[YYYY-MM-DD HH:MM] …`` block rather than
        overwriting, so a later session never loses an earlier one's note
        (open question 2 — append-only with timestamp prefixes). Returns the
        full updated context. Refuses a blank entry — an empty note carries no
        durable knowledge and would just add noise.
        """
        text = entry.strip()
        if not text:
            msg = "context entry is empty"
            raise ValueError(msg)
        stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            updated = f"{locked.context}\n\n[{stamp}] {text}"
            self.context = updated
            type(self).objects.filter(pk=self.pk).update(context=updated)
        return updated


def render_ticket_context(context: str, *, max_lines: int = 40) -> str:
    """Render ``Ticket.context`` as a collapsed intake section (#627).

    Returns a leading-newline-prefixed GitHub-style ``<details>`` block so
    the next session sees the durable knowledge without an explicit lookup,
    while the intake output stays scannable. The block is appended directly
    after the intake summary's last line, so a single ``str`` (rather than a
    line list) keeps the call site one branch-free statement. Long stores
    are truncated with a pointer to ``ticket context show``. An empty store
    renders the empty string — nothing is shown.
    """
    body = context.strip()
    if not body:
        return ""
    entries = body.splitlines()
    shown = entries[:max_lines]
    lines = ["", "", "<details>", "<summary>Ticket context (durable knowledge store)</summary>", "", *shown]
    if len(entries) > max_lines:
        hidden = len(entries) - max_lines
        lines.extend(["", f"… ({hidden} more line(s) truncated — `t3 <overlay> ticket context show`)"])
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def format_intake_summary(ticket: Ticket, ticket_dir: str, branch: str) -> str:
    """Format the ``workspace ticket`` intake summary block (#627).

    Worktree list, ticket header, branch, and the collapsed durable-context
    section, returned as one string. The ticket-display concern lives here
    next to :func:`render_ticket_context` rather than inflating the already
    at-capacity ``workspace`` command module (module-health split-by-concern).
    """
    lines = [f"  {wt.repo_path}: worktree #{wt.pk}" for wt in ticket.worktrees.all()]  # ty: ignore[unresolved-attribute]
    lines.extend(
        (
            f"\nTicket #{ticket.pk} — worktrees in {ticket_dir}",
            f"  Branch: {branch}{render_ticket_context(ticket.context)}",
        )
    )
    return "\n".join(lines)


def schedule_external_review(ticket: Ticket, *, parent_task: "Task | None" = None) -> "Task":
    """Create a reviewing task for a reviewer-role ticket (external PR).

    Reviewer-role tickets represent PRs the user is requested to review
    in someone else's repo — they have no implementation/test/ship
    phases. After the review task completes, the ticket short-circuits
    to ``DELIVERED`` via ``mark_reviewed_externally``.

    Lives at module scope (not on ``Ticket``) to keep the model's
    public-method count under the project's lint cap; semantically it is
    a sibling of ``ticket.schedule_coding`` and friends.
    """
    from teatree.core.models.session import Session  # noqa: PLC0415
    from teatree.core.models.task import Task  # noqa: PLC0415

    if ticket.role != Ticket.Role.REVIEWER:
        msg = f"schedule_external_review requires role=reviewer (got role={ticket.role!r})"
        raise InvalidTransitionError(msg)
    session = Session.objects.create(ticket=ticket, agent_id="external-review")
    return Task.objects.create(
        ticket=ticket,
        session=session,
        phase="reviewing",
        execution_target=Task.ExecutionTarget.HEADLESS,
        execution_reason=f"Auto-scheduled external review — review {ticket.issue_url}",
        parent_task=parent_task,
    )


def _worktree_has_commits_ahead(worktree: "Worktree") -> bool:
    repo_path = (worktree.extra or {}).get("worktree_path") or worktree.repo_path
    branch = worktree.branch
    if not repo_path or not branch:
        return False
    base = _resolve_base_branch(repo_path)
    try:
        return git.rev_count(repo=repo_path, range_spec=f"{base}..{branch}") > 0
    except (CommandFailedError, ValueError, OSError):
        # Missing path, missing branch, missing git remote — all mean no
        # shippable diff. Fail closed so the auto-FSM stops at REVIEWED.
        return False


def _worktree_tracked_dirty_path(worktree: "Worktree") -> str | None:
    """Return the worktree's on-disk path iff it has uncommitted *tracked* changes.

    Reuses the existing :func:`git.status_porcelain` helper (the same one
    ``cleanup`` / ``worktree_recovery`` use) and applies the #925
    tracked-vs-untracked distinction: ``git status --porcelain`` prefixes
    an untracked entry with ``"?? "``, so lines that do *not* start with
    ``??`` are the tracked modifications a transition must refuse. Untracked
    scratch never blocks (the loop legitimately leaves it around, and a
    fast-forward never conflicts with it).

    Path resolution mirrors :func:`_worktree_has_commits_ahead`
    (``extra['worktree_path']`` then ``repo_path``). An unresolvable or
    non-git path returns ``None`` (not dirty): the guard must not block on
    a state it cannot verify — "couldn't determine" is not "is dirty", and
    over-blocking a legitimately-clean ticket would stall the loop.
    """
    repo_path = (worktree.extra or {}).get("worktree_path") or worktree.repo_path
    if not repo_path:
        return None
    try:
        porcelain = git.status_porcelain(repo_path)
    except (CommandFailedError, OSError):
        return None
    tracked_dirty = any(line and not line.startswith("??") for line in porcelain.splitlines())
    return repo_path if tracked_dirty else None


def _resolve_base_branch(repo_path: str) -> str:
    try:
        return f"origin/{git.default_branch(repo_path)}"
    except (CommandFailedError, RuntimeError):
        # No origin remote (fresh clones, tests under tmp_path) — fall back to
        # the local default. ``RuntimeError`` covers ``default_branch``'s own
        # "could not detect" path.
        return "main"
