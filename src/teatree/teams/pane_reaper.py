"""Idle-pane reaper for the inert maker-only pane layer (#1838 PR#7a).

A sibling of the idle-stack reaper (:mod:`teatree.core.gates.idle_stack`): each
scan demotes any maker pane — a ``team:<role>`` claim of a
:class:`~teatree.core.models.task.Task` — that has had NO live Session driving
it for longer than ``teams_idle_minutes`` to STOPPED. "Demote" releases the
``team:<role>`` claim (the pane's terminal :class:`~teatree.teams.panes.PaneState.STOPPED`),
freeing the slot for a future spawn. The DB lease primitives
(``reclaim_orphaned_claims`` / ``reap_stale_claims``) recover a dead pane's
claim independently; this reaper is the GRACEFUL idle path on top.

FAIL-SAFE doctrine (mirrors :mod:`idle_stack`): every uncertainty resolves to
KEEP. A null heartbeat cannot be confirmed idle, so the pane is kept; a pane
whose ticket still has a live :class:`~teatree.core.models.session.Session` is
kept; only a confirmed-stale, no-live-session pane is reaped.

Inert: nothing in the live loop / dispatch / claim path imports this module
while the pane layer ships dark (the #2320 AST inertness scan pins that). A
LATER PR (#7b) registers this as a mini-loop scanner — the registration is the
consumer-side wiring, deferred so a ``loops/`` module never imports
``teatree.teams`` while the feature is off.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta

from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.teams.roles import TEAM_CLAIM_PREFIX


def _ticket_has_live_session(task: Task) -> bool:
    """True iff the pane's ticket still has a live (un-ended) Session driving it."""
    return Session.objects.filter(ticket=task.ticket, ended_at__isnull=True).exists()


def _is_idle_reapable(task: Task, *, cutoff: datetime) -> bool:
    """Apply the full fail-safe idle-pane predicate to one claimed pane task.

    KEEP (return ``False``) on every uncertainty: a null heartbeat (cannot
    confirm idle), a heartbeat newer than the cutoff (fresh), or a ticket with
    a live Session (still being driven). Only a confirmed-stale, no-live-session
    pane is reapable.
    """
    if task.heartbeat_at is None:
        return False
    if task.heartbeat_at > cutoff:
        return False
    return not _ticket_has_live_session(task)


def reapable_panes(*, idle_minutes: int, now: datetime | None = None) -> Iterator[Task]:
    """Yield the idle maker-pane tasks that should be demoted to STOPPED.

    A candidate is a CLAIMED task whose ``claimed_by`` is in the ``team:``
    namespace. ``idle_minutes`` is the staleness threshold; a pane whose
    ``heartbeat_at`` is older than ``now - idle_minutes`` AND whose ticket has
    no live Session is yielded. Caller-supplied *now* is the test/clock seam.
    """
    moment = now or timezone.now()
    cutoff = moment - timedelta(minutes=idle_minutes)
    candidates = (
        Task.objects.filter(status=Task.Status.CLAIMED, claimed_by__startswith=TEAM_CLAIM_PREFIX)
        .select_related("ticket")
        .order_by("pk")
    )
    for task in candidates:
        if _is_idle_reapable(task, cutoff=cutoff):
            yield task


def reap_idle_panes(*, idle_minutes: int, now: datetime | None = None) -> int:
    """Demote every idle maker pane to STOPPED. Returns the number reaped (#1838 PR#7a).

    For each :func:`reapable_panes` candidate the claim is released — the same
    terminal effect as :meth:`teatree.teams.panes.TeammatePane.stop`, reached
    via a backend-agnostic conditional UPDATE re-asserting the team-claim +
    CLAIMED predicate so a concurrent heartbeat/stop between the scan and the
    write is not clobbered. The pane's derived state then reads STOPPED.
    """
    reaped = 0
    for task in list(reapable_panes(idle_minutes=idle_minutes, now=now)):
        released = (
            Task.objects.filter(pk=task.pk, status=Task.Status.CLAIMED, claimed_by=task.claimed_by)
            .filter(claimed_by__startswith=TEAM_CLAIM_PREFIX)
            .update(
                status=Task.Status.PENDING,
                claimed_at=None,
                claimed_by="",
                lease_expires_at=None,
                heartbeat_at=None,
            )
        )
        reaped += released
    return reaped


__all__ = ["reap_idle_panes", "reapable_panes"]
