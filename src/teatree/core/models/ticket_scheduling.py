import logging
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.config import Mode, get_effective_settings
from teatree.core.modelkit.gate_registry import get_gate
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.modelkit.task_failure_taxonomy import SUPERSEDED_PREFIX
from teatree.core.models.errors import DirtyWorktreeError, InvalidTransitionError
from teatree.core.models.plan_decision import has_plan_decision, refuse_unplanned_mint
from teatree.core.models.ticket_data import TicketFacet
from teatree.core.models.ticket_worktree_checks import collect_dirty_worktree_paths

if TYPE_CHECKING:
    from teatree.core.managers import TaskQuerySet
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

#: ``Ticket.extra`` key carrying the phase and reason a producer asked for across the planning rung.
PLANNING_HANDOFF_KEY = "planning_handoff"


def _auto_ship_enabled() -> bool:
    return get_effective_settings().mode == Mode.AUTO


class TicketSchedulingModel(TicketFacet):
    """Fresh-session phase-task scheduling, orphan-task consumption, and the dirty-worktree preflight."""

    if TYPE_CHECKING:
        # Reverse accessor for ``Task.ticket``'s ``related_name="tasks"`` — Django
        # synthesises it at class-prep time, invisible to a static checker.
        tasks: "TaskQuerySet"

    class Meta:
        abstract = True

    def schedule_planning(self: "Ticket", *, parent_task: "Task | None" = None, intent: str = "") -> "Task":
        """Create a fresh headless planning task after provisioning completes."""
        reason = "Auto-scheduled planning — produce a plan before coding"
        if intent.strip():
            reason = f"{reason}\n\nThe work this plan is for:\n{intent.strip()}"
        return self._schedule_phase_task("planning", reason, parent_task, require_author=True)

    def schedule_plan_reaffirm(self: "Ticket", *, refusal: str, parent_task: "Task | None" = None) -> "Task":
        """The planning pass a plan the currency gate refused is owed: reaffirm it against what moved, or re-plan."""
        return self.schedule_planning(
            parent_task=parent_task,
            intent=(
                f"The plan is not current: {refusal} Record a per-commit disposition (no conflict / compatible / "
                "conflict). If none conflicts, re-emit the plan at the new base_sha with that section; if one "
                "conflicts, re-plan."
            ),
        )

    def begin_planning(self: "Ticket", *, parent_task: "Task | None" = None, intent: str = "") -> "Task":
        """Walk an early-state author ticket up to WORK_STARTED and schedule its planning task.

        The transitions are load-bearing, not decoration: ``Ticket.plan``'s FSM source is
        exclusively WORK_STARTED, and ``Task._apply_phase_transition``'s planning branch is
        guarded on the same state — so a planning task scheduled on a NOT_STARTED ticket
        completes into ``escalate_unmatched_phase_transition`` and never reaches
        ``plan()`` -> ``schedule_coding()``. Scheduling planning WITHOUT them is the shape
        of the hourly wedge this replaces (souliane/teatree#4578).

        Idempotent: past WORK_STARTED the ladder walk is skipped and ``schedule_planning``'s
        CAS returns the in-flight sibling, so a repeated call mints nothing. A ticket
        already past PLAN_RECORDED has no planning left to begin and is refused, so a
        mis-routed caller fails loudly rather than minting a phase task the FSM will
        never consume.

        The guard, the walk and the mint all read the ``select_for_update`` re-read
        (#883/#804 discipline, as ``Task._apply_phase_transition``): the drain
        materialises its candidate list first, so ``self`` is a snapshot by the time it
        arrives here. Guarding on the snapshot's state walks a concurrently-advanced
        ticket back down the ladder, and persisting through a full-row ``save`` writes
        the snapshot's ``extra`` back over a key another writer has since recorded —
        hence ``merge_extra``, whose own locked re-read carries the state.
        """
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if locked.state not in self.EARLY_STATES:
                msg = (
                    f"begin_planning requires an early state {sorted(self.EARLY_STATES)!r} (got state={locked.state!r})"
                )
                raise InvalidTransitionError(msg)
            locked.walk_to_work_started()
            locked.merge_extra(also_set={"state": locked.state})
            return locked.schedule_planning(parent_task=parent_task, intent=intent)

    def walk_to_work_started(self: "Ticket") -> None:
        """Fire ``scope`` / ``start`` so a NOT_STARTED or SCOPED ticket sits where ``plan()`` can consume it."""
        if self.state == self.State.NOT_STARTED:
            self.scope()
        if self.state == self.State.SCOPED:
            self.start()

    def admits_implementing(self: "Ticket") -> bool:
        """Whether :meth:`schedule_implementing` can take work: a plan decision exists, or planning can still begin."""
        return has_plan_decision(self) or self.state in self.EARLY_STATES

    def schedule_implementing(self: "Ticket", phase: str, *, reason: str, parent_task: "Task | None" = None) -> "Task":
        """Mint *phase* on a planned ticket; route an unplanned early one to planning carrying *reason*.

        The routed request is recorded as the planning task's hand-off, so ``plan()``
        mints *phase* with *reason* — a debugging or e2e fix keeps its own agent through
        planning. Past the early states an unplanned ticket has no planning rung left to
        take, so the mint seam refuses it with ``NoPlanArtifactError``.
        """
        if has_plan_decision(self) or self.state not in self.EARLY_STATES:
            return self._schedule_phase_task(phase, reason, parent_task)
        planning = self.begin_planning(parent_task=parent_task, intent=reason)
        handoff = (self.extra or {}).get(PLANNING_HANDOFF_KEY) or {}
        if handoff.get("planning_task") != planning.pk:
            self.merge_extra(
                set_keys={
                    PLANNING_HANDOFF_KEY: {
                        "planning_task": planning.pk,
                        "phase": normalize_phase(phase),
                        "reason": reason,
                    }
                }
            )
        return planning

    def schedule_planned_work(self: "Ticket", *, parent_task: "Task | None" = None) -> "Task":
        """The first implementing task after planning: the phase a producer routed here for, else coding.

        The hand-off is spent by the mint, so a later re-plan starts from generic coding.
        """
        handoff = (self.extra or {}).get(PLANNING_HANDOFF_KEY)
        if not handoff or not handoff.get("phase"):
            return self.schedule_coding(parent_task=parent_task)
        phase = str(handoff["phase"])
        task = self._schedule_phase_task(
            phase,
            str(handoff.get("reason") or f"Auto-scheduled {phase} — carry out the planned work"),
            parent_task,
            require_author=True,
            gate="plan_currency" if normalize_phase(phase) == "coding" else None,
        )
        self.merge_extra(pop_keys=[PLANNING_HANDOFF_KEY])
        return task

    def schedule_coding(self: "Ticket", *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless coding task after planning completes.

        Gated by ``plan_currency`` (SELFCATCH-3) on the normal author PLAN_RECORDED→CODED flow
        (the same gate ``code()`` runs): no coding task for a thin/legacy or seam-stale
        plan. Synthetic corrective re-entries that mint a coding task directly are
        exempt; they route through :meth:`schedule_implementing`.
        """
        return self._schedule_phase_task(
            "coding",
            "Auto-scheduled coding — implement the ticket",
            parent_task,
            require_author=True,
            gate="plan_currency",
        )

    def _schedule_phase_task(
        self: "Ticket",
        phase: str,
        reason: str,
        parent_task: "Task | None",
        *,
        require_author: bool = False,
        gate: str | None = None,
    ) -> "Task":
        """Shared fresh-session headless scheduler for the auto-FSM phase tasks.

        Optionally enforces ``role=author`` and runs an FSM ``gate`` (the
        plan-currency leak-close), then mints the ``phase`` Session + headless Task.
        The session ``agent_id`` is the ``phase`` (``reviewing`` uses ``review``).
        An implementing phase on a ticket with no plan decision raises ``NoPlanArtifactError``
        before anything is written.

        **Idempotent in its side effects, not merely in the state it converges to**
        (#3903). An in-flight sibling — a PENDING or CLAIMED Task on the same
        ``(ticket, phase)``, in any accepted spelling — is RETURNED rather than
        raced: two coding Tasks were once claimed and dispatched concurrently
        against one worktree because the only guards were read-then-write checks in
        the callers, and one of them raced. The dedupe lock is
        :meth:`TaskQuerySet.in_flight_for_phase`, the same SSOT the periodic
        scanners read, consulted here at the write.

        Callers keep their own pre-checks (``loop/persistence.py``,
        ``loops/outer_loop/implement.py``, ``loops/directive_loop/implement.py``):
        they short-circuit before doing useless
        setup work, which is worth having. What changed is that they are no longer
        load-bearing for CORRECTNESS — a caller that forgets one, or whose
        read-then-write races, can no longer mint a rival task, because this seam
        checks under the same lock it writes in.

        The check and the mint share one ``atomic`` block, so the guard is a real
        CAS: SQLite is opened in ``transaction_mode="IMMEDIATE"``, so the first
        writer holds the reserved lock for the whole block and a concurrent tick
        cannot pass the same check.

        A TERMINAL sibling is not a duplicate. A COMPLETED, FAILED or CANCELLED
        task is a finished attempt, so the next call mints a fresh Session + Task —
        the rework paths (``_cancel_pending_tasks``) fail their active tasks first
        precisely so a genuine second attempt is never swallowed by this guard.
        """
        from teatree.core.models.session import Session  # noqa: PLC0415 — import cycle
        from teatree.core.models.task import Task  # noqa: PLC0415 — import cycle

        if require_author and self.role != self.Role.AUTHOR:
            msg = f"schedule_{phase} requires role=author (got role={self.role!r})"
            raise InvalidTransitionError(msg)
        refuse_unplanned_mint(self, phase=phase)
        if gate is not None:
            get_gate(gate)(self)
        with transaction.atomic():
            in_flight = Task.objects.in_flight_for_phase(self.overlay, phase).filter(ticket=self).order_by("pk").first()
            if in_flight is not None:
                logger.info(
                    "schedule_%s reused in-flight task %s for ticket %s (status=%s)",
                    phase,
                    in_flight.pk,
                    self.pk,
                    in_flight.status,
                )
                return in_flight
            session = Session.objects.create(
                ticket=self, agent_id="review" if normalize_phase(phase) == "reviewing" else phase
            )
            return Task.objects.create(
                ticket=self,
                session=session,
                phase=phase,
                execution_reason=reason,
                parent_task=parent_task,
            )

    def schedule_testing(self: "Ticket", *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless testing task after coding completes."""
        return self._schedule_phase_task("testing", "Auto-scheduled testing — run + QA the coding work", parent_task)

    def schedule_review(self: "Ticket", *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless review+retro task (new session for bias-free evaluation)."""
        return self._schedule_phase_task(
            "reviewing", "Auto-scheduled review + retro — fresh agent, no bias", parent_task
        )

    def schedule_shipping(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create an INTERACTIVE shipping task; approval gating rides the reason.

        Shipping is a loop-dispatched phase (``(author, shipping)`` →
        ``t3:shipper``), so it runs as an in-session sub-agent
        (subscription-covered), never a metered detached headless-SDK run — regardless of
        auto mode. Auto mode no longer changes the execution *target*; it only
        changes the *approval posture* the in-session shipper reads from
        ``execution_reason`` (auto = push without waiting; otherwise = gate for
        user approval first).
        """
        from teatree.core.models.session import Session  # noqa: PLC0415 — import cycle
        from teatree.core.models.task import Task  # noqa: PLC0415 — import cycle

        session = Session.objects.create(ticket=self, agent_id="shipping")
        if _auto_ship_enabled():
            reason = "Auto-scheduled shipping — auto mode, push will proceed without waiting for approval"
        else:
            reason = (
                "Auto-scheduled shipping — gated for user approval "
                "(set mode = auto via config_setting set, or T3_MODE=auto, to skip)"
            )
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="shipping",
            execution_reason=reason,
            parent_task=parent_task,
        )

    def _cancel_pending_tasks(self) -> None:
        """Fail all pending/claimed tasks when reworking, each naming rework as its cause."""
        from teatree.core.models.task import Task  # noqa: PLC0415 — import cycle

        for task in self.tasks.filter(status__in=Task.Status.active()):  # type: ignore[attr-defined]  # Django reverse FK
            task.fail(
                reason=f"{SUPERSEDED_PREFIX}ticket reworked — this task's phase is being redone",
                by_holder=False,
            )

    def _refuse_if_worktree_dirty(self: "Ticket", phase: str) -> None:
        """Preflight gate (#884): refuse the transition if a worktree is tracked-dirty.

        Run at the top of the ``code``/``test``/``review``/``ship``
        transition bodies. Dirty-collection rule and the no-auto-stash/
        lease-reaper rationale live on :func:`collect_dirty_worktree_paths`
        (#1983 LOC-ratchet split). On dirty: a loud :class:`DirtyWorktreeError`
        names the dirty worktree(s) and the transition does not advance —
        every production caller wraps the transition body in an outer
        ``transaction.atomic``, so the raise rolls that whole atomic back.
        """
        dirty = collect_dirty_worktree_paths(self)
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

        The bulk ``UPDATE`` is deliberate — a per-row ``save()`` would re-enter
        the ``post_save`` headless auto-enqueue — so the sessions the terminal
        task's ``post_save`` receiver would have closed are closed here instead;
        without that the orphaned task's session stays open forever, which is the
        very zombie this consume exists to prevent.
        """
        from teatree.core.models.session import Session  # noqa: PLC0415 — import cycle
        from teatree.core.models.task import Task  # noqa: PLC0415 — import cycle

        consumed = Task.objects.pending_in_phase(phase).filter(ticket=self)
        sessions = list(Session.objects.filter(pk__in=consumed.values("session_id")))
        consumed.update(
            status=Task.Status.COMPLETED,
            claimed_at=None,
            claimed_by="",
            lease_expires_at=None,
            heartbeat_at=None,
        )
        for session in sessions:
            session.close_if_idle()
