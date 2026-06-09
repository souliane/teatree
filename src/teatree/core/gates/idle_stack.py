"""Idle-stack detection for the idle-stack reaper (souliane/teatree#2190).

A locally-running worktree (``SERVICES_UP`` / ``READY``) holds a docker stack,
language servers, browsers and CI processes — and a
``max_concurrent_local_stacks`` slot. When that worktree's ticket is IDLE, the
stack needlessly holds RAM and the slot, stalling new work. The idle-stack
reaper stops such a stack (``Worktree.stop_services`` → demote to
``PROVISIONED``, REVERSIBLE — DB + worktree preserved) to free the slot.

A worktree is REAPABLE when ALL hold: (1) its state is ``SERVICES_UP`` or
``READY`` — a dormant ``PROVISIONED`` row holds no stack, so it is never a
candidate; (2) its ticket has NO live :class:`Session` (``ended_at`` is null)
and NO active/claimed :class:`Task` (``PENDING`` / ``CLAIMED``); (3)
``last_used_at`` is older than the idle threshold — a null ``last_used_at``
cannot be confirmed idle, so it is a fail-safe KEEP; (4) it is not the
currently-active worktree (the process CWD lives inside it).

FAIL-SAFE doctrine: every uncertainty resolves to KEEP. A db-only partial
stack (the wt595 leak class — app tier down but a stray ``db-1`` lingering) is
reapable, not "healthy": ``stop_services`` brings the WHOLE compose project
down so no stray container survives.
"""

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

from django.db.models import Q
from django.utils import timezone
from django_fsm import can_proceed

from teatree.core.models import Session, Task, Worktree
from teatree.core.worktree_env import compose_project
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

_RUNNING_STATES: tuple[str, ...] = (Worktree.State.SERVICES_UP, Worktree.State.READY)
_ACTIVE_TASK_STATES: tuple[str, ...] = (Task.Status.PENDING, Task.Status.CLAIMED)


def _running_container_count(project: str) -> int:
    """Count running docker containers for *project*.

    Used only for an advisory partial-stack log line — the reaper does NOT
    gate reaping on this count (a db-only partial stack is reapable, and an
    already-gone stack is reaped idempotently). Returns ``-1`` when docker
    cannot be queried so the log line distinguishes "verified empty" from
    "could not verify".
    """
    if not project:
        return -1
    cmd = [
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--format",
        "{{.Names}}",
    ]
    result = run_allowed_to_fail(cmd, expected_codes=None)
    if result.returncode != 0:
        return -1
    return sum(1 for name in result.stdout.splitlines() if name.strip())


def _active_worktree_path() -> Path | None:
    """The on-disk worktree the current process is operating inside, or ``None``."""
    try:
        return Path.cwd().resolve()
    except OSError:
        return None


def _is_currently_active(worktree: Worktree, active_path: Path | None) -> bool:
    """True iff *active_path* is the worktree's own dir or a child of it."""
    if active_path is None:
        return False
    wt_path = worktree.worktree_path
    if not wt_path:
        return False
    resolved = Path(wt_path).resolve()
    return active_path == resolved or resolved in active_path.parents


def _ticket_is_busy(worktree: Worktree) -> bool:
    """True iff the worktree's ticket has a live session or an active/claimed task."""
    ticket = worktree.ticket
    if Session.objects.filter(ticket=ticket, ended_at__isnull=True).exists():
        return True
    return Task.objects.filter(ticket=ticket, status__in=_ACTIVE_TASK_STATES).exists()


def _is_reapable(worktree: Worktree, *, cutoff: datetime, active_path: Path | None) -> bool:
    """Apply the full fail-safe reapability predicate to one worktree."""
    if not can_proceed(worktree.stop_services):
        return False
    if worktree.last_used_at is None:
        return False
    if worktree.last_used_at > cutoff:
        return False
    if _ticket_is_busy(worktree):
        return False
    if _is_currently_active(worktree, active_path):
        return False
    running = _running_container_count(compose_project(worktree))
    if running == 0:
        logger.info("idle_stack: worktree %s has zero running containers — reaping (idempotent)", worktree.repo_path)
    return True


def reapable_worktrees(*, overlay: str, idle_minutes: int, now: datetime | None = None) -> Iterator[Worktree]:
    """Yield the idle running worktrees of *overlay* that should be reaped.

    Scoped per overlay (mirroring ``check_local_stack_limit``). ``idle_minutes``
    is the staleness threshold; a worktree whose ``last_used_at`` is older than
    ``now - idle_minutes`` AND whose ticket is not busy AND which is not the
    active worktree is yielded. Caller-supplied *now* is the test/clock seam.
    """
    moment = now or timezone.now()
    cutoff = moment - timedelta(minutes=idle_minutes)
    active_path = _active_worktree_path()
    candidates = (
        Worktree.objects.filter(overlay=overlay, state__in=_RUNNING_STATES)
        .filter(Q(last_used_at__isnull=False))
        .select_related("ticket")
        .order_by("pk")
    )
    for worktree in candidates:
        if _is_reapable(worktree, cutoff=cutoff, active_path=active_path):
            yield worktree


__all__ = ["reapable_worktrees"]
