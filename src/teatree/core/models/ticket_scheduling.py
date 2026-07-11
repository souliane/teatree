from typing import TYPE_CHECKING

from teatree.config import Mode, get_effective_settings
from teatree.core.modelkit.gate_registry import get_gate
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.errors import DirtyWorktreeError, InvalidTransitionError
from teatree.core.models.ticket_data import TicketFacet
from teatree.core.models.ticket_worktree_checks import collect_dirty_worktree_paths

if TYPE_CHECKING:
    from teatree.core.models.session import Session
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket


def _auto_ship_enabled() -> bool:
    return get_effective_settings().mode == Mode.AUTO


class TicketSchedulingModel(TicketFacet):
    """Fresh-session phase-task scheduling, orphan-task consumption, and the dirty-worktree preflight."""

    class Meta:
        abstract = True

    def schedule_planning(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless planning task after provisioning completes."""
        return self._schedule_headless(
            "planning", "Auto-scheduled planning — produce a plan before coding", parent_task, require_author=True
        )

    def schedule_coding(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless coding task after planning completes.

        Gated by ``plan_currency`` (SELFCATCH-3) on the normal author PLANNED→CODED flow
        (the same gate ``code()`` runs): no coding task for a thin/legacy or seam-stale
        plan. NO-OP unless ``require_plan_adequacy`` is on; synthetic corrective
        re-entries that mint a coding task directly are exempt (they carry no plan).
        """
        return self._schedule_headless(
            "coding",
            "Auto-scheduled coding — implement the ticket",
            parent_task,
            require_author=True,
            gate="plan_currency",
        )

    def _schedule_headless(
        self,
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
        """
        from teatree.core.models.session import Session  # noqa: PLC0415 — import cycle
        from teatree.core.models.task import Task  # noqa: PLC0415 — import cycle

        if require_author and self.role != self.Role.AUTHOR:
            msg = f"schedule_{phase} requires role=author (got role={self.role!r})"
            raise InvalidTransitionError(msg)
        if gate is not None:
            get_gate(gate)(self)
        session = Session.objects.create(
            ticket=self, agent_id="review" if normalize_phase(phase) == "reviewing" else phase
        )
        return Task.objects.create(
            ticket=self,
            session=session,
            phase=phase,
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason=reason,
            parent_task=parent_task,
        )

    def schedule_testing(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless testing task after coding completes."""
        return self._schedule_headless("testing", "Auto-scheduled testing — run + QA the coding work", parent_task)

    def schedule_review(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless review+retro task (new session for bias-free evaluation)."""
        return self._schedule_headless("reviewing", "Auto-scheduled review + retro — fresh agent, no bias", parent_task)

    def schedule_review_in_session(self, session: "Session", *, parent_task: "Task | None" = None) -> "Task":
        """Create a review task within an existing session (sub-agent, not a new session)."""
        from teatree.core.models.task import Task  # noqa: PLC0415 — import cycle

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
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            execution_reason=reason,
            parent_task=parent_task,
        )

    def _cancel_pending_tasks(self) -> None:
        """Fail all pending/claimed tasks when reworking."""
        from teatree.core.models.task import Task  # noqa: PLC0415 — import cycle

        for task in self.tasks.filter(status__in=Task.Status.active()):  # type: ignore[attr-defined]  # Django reverse FK
            task.fail()

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
        """
        from teatree.core.models.task import Task  # noqa: PLC0415 — import cycle

        Task.objects.pending_in_phase(phase).filter(ticket=self).update(
            status=Task.Status.COMPLETED,
            claimed_at=None,
            claimed_by="",
            lease_expires_at=None,
            heartbeat_at=None,
        )
