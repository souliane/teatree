"""Pane lifecycle FSM over the existing ``Task`` + lease (#1838 PR#7a).

A maker pane is a long-lived ``team:<role>`` claim of a :class:`~teatree.core.models.task.Task`.
PR#6 already established that a teammate is just another ``Task.claimed_by``
value (``team:<role>``) — no new model, no migration. PR#7a adds the LIFECYCLE on
top of that claim, computed from the existing rows rather than a new column:

``spawn → active → idle → stopped``

``ACTIVE`` — the task carries a live ``team:<role>`` claim (CLAIMED + an unexpired
lease) AND a live :class:`~teatree.core.models.session.Session` drives the ticket.
``IDLE`` — the claim is recorded but no live Session/Task is driving the pane (the
session ended, or the lease lapsed); the idle reaper demotes such a pane.
``STOPPED`` — the claim has been cleared (a graceful ``TeammateIdle`` stop, or a
reaper demotion). The DB lease primitives (``reclaim_orphaned_claims`` /
``reap_stale_claims``) reach the same terminal state for a dead pane, so the DB
stays the single source of truth.

Inert: nothing in the live loop / dispatch / claim path imports this module while
the pane layer ships dark (the #2320 AST inertness scan pins that). A LATER PR
(#7b) drives a real pane through these transitions.
"""

from enum import Enum

from teatree.core.models.task import Task
from teatree.teams.guardrails import assert_pane_claim_allowed
from teatree.teams.roles import TeamRole, team_claim_slot


class PaneState(Enum):
    """The derived lifecycle state of a maker pane."""

    SPAWN = "spawn"
    ACTIVE = "active"
    IDLE = "idle"
    STOPPED = "stopped"


class TeammatePane:
    """A maker pane: a long-lived ``team:<role>`` claim of one ``Task``.

    Thin lifecycle wrapper — it owns NO state of its own beyond the task pk and
    the canonical claim slot; the lifecycle :class:`PaneState` is always DERIVED
    from the live row (:meth:`refreshed_state`), so the DB is the single source
    of truth and a stale in-memory pane can never report a phantom state.
    """

    def __init__(self, task: Task, *, role: TeamRole) -> None:
        self._task = task
        self._claim_slot = team_claim_slot(role)

    @property
    def claim_slot(self) -> str:
        """The canonical ``team:<role>`` claim key this pane holds."""
        return self._claim_slot

    @property
    def state(self) -> PaneState:
        """The pane's lifecycle state as of the in-memory task snapshot."""
        return self._derive_state(self._task)

    @classmethod
    def spawn(cls, task: Task, *, role: TeamRole, lease_seconds: int = 300) -> "TeammatePane":
        """Claim *task* under ``team:<role>`` and return the ACTIVE pane (#1838 PR#7a).

        The claim runs the namespace guard (:func:`assert_pane_claim_allowed`)
        first, so a pane can never claim anything but its own ``team:<role>``
        slot — the loop-owner collision is impossible by construction. The
        existing ``Task.claim`` CAS is the spawn primitive, so a spawned pane
        participates in the same lease lifecycle (``renew_lease`` heartbeat,
        ``reclaim_orphaned_claims`` / ``reap_stale_claims`` recovery) as any
        other claim.
        """
        slot = team_claim_slot(role)
        assert_pane_claim_allowed(slot)
        task.claim(claimed_by=slot, lease_seconds=lease_seconds)
        return cls(task, role=role)

    def heartbeat(self, *, lease_seconds: int = 300) -> None:
        """Renew the pane's lease via the existing ``Task.renew_lease`` heartbeat.

        A heartbeated pane keeps a live lease so ``reap_stale_claims`` /
        ``reclaim_orphaned_claims`` never recover its claim out from under a
        live teammate — and a pane that STOPS heartbeating is recovered by
        exactly those sweeps. The DB lease is the source of truth.
        """
        self._task.renew_lease(lease_seconds=lease_seconds)

    def stop(self, *, reason: str = "") -> None:
        """Graceful stop ("TeammateIdle"): release the ``team:<role>`` claim.

        Idempotent — clearing an already-cleared claim is a no-op (the task is
        no longer CLAIMED under this slot). Demotes the pane to ``STOPPED``;
        the DB row's claim fields are zeroed so the slot is free for a future
        spawn and the reaper sees the terminal state.
        """
        del reason  # The reason is a caller-facing label; the DB effect is the same release.
        self._task.refresh_from_db()
        if self._task.claimed_by == self._claim_slot and self._task.status == Task.Status.CLAIMED:
            self._task.status = Task.Status.PENDING
            self._task.claimed_at = None
            self._task.claimed_by = ""
            self._task.lease_expires_at = None
            self._task.heartbeat_at = None
            self._task.save(
                update_fields=["status", "claimed_at", "claimed_by", "lease_expires_at", "heartbeat_at"],
            )

    def refreshed_state(self) -> PaneState:
        """Re-read the live task row and return the derived lifecycle state."""
        self._task.refresh_from_db()
        return self._derive_state(self._task)

    def _derive_state(self, task: Task) -> PaneState:
        """Compute the lifecycle state from the live task + its ticket's sessions.

        ``STOPPED`` when the claim is gone; ``ACTIVE`` when the claim is live
        (CLAIMED + unexpired lease) AND a live Session drives the ticket;
        ``IDLE`` otherwise (claim recorded but no live driver). The pid-anchored
        / lease-anchored liveness comes from the existing claim fields, so the
        FSM never invents a parallel liveness notion.
        """
        if task.claimed_by != self._claim_slot or task.status != Task.Status.CLAIMED:
            return PaneState.STOPPED
        if self._claim_is_live(task) and self._ticket_has_live_session(task):
            return PaneState.ACTIVE
        return PaneState.IDLE

    @staticmethod
    def _claim_is_live(task: Task) -> bool:
        from django.utils import timezone  # noqa: PLC0415

        return task.lease_expires_at is not None and task.lease_expires_at > timezone.now()

    @staticmethod
    def _ticket_has_live_session(task: Task) -> bool:
        from teatree.core.models.session import Session  # noqa: PLC0415

        return Session.objects.filter(ticket=task.ticket, ended_at__isnull=True).exists()


__all__ = ["PaneState", "TeammatePane"]
