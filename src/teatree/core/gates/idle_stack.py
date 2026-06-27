"""Idle-stack detection for the idle-stack reaper (souliane/teatree#2190, #2227).

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
currently-active worktree (the process CWD lives inside it); (5) #2227 — its
ticket carries NO live external-delivery lease (the same lease that gates
dispatch), it has NOT been touched by an E2E/evidence run within
``idle_stack_e2e_recent_minutes`` (``Worktree.last_e2e_run``), and it is NOT
explicitly pinned (``extra['reaper_pinned']``). A stack under active delivery
or holding fresh evidence is the live target of in-flight work — reaping it
forces a slow re-provision.

FAIL-SAFE doctrine: every uncertainty resolves to KEEP. A db-only partial
stack (the wt595 leak class — app tier down but a stray ``db-1`` lingering) is
reapable, not "healthy": ``stop_services`` brings the WHOLE compose project
down so no stray container survives.

:func:`preserve_reason` is the single predicate: it returns the human-readable
reason a worktree is KEPT, or ``None`` when it is reapable.
:func:`classify_running_worktrees` yields ``(worktree, reason)`` for every
running candidate so the reaper's tick log can surface preserved-vs-reaped — a
reap is never silent. :func:`reapable_worktrees` is the ``reason is None``
filter over that classification.
"""

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

from django.db.models import Q
from django.utils import timezone
from django_fsm import can_proceed

from teatree.config import get_effective_settings
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.models.external_delivery import under_external_delivery
from teatree.core.models.types import validated_worktree_extra
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


def ticket_is_busy(ticket: Ticket) -> bool:
    """True iff *ticket* has a live session or an active/claimed task.

    The ticket-level half of the liveness signal. Reapers do not call this
    directly — they call :func:`worktree_protects_against_reap`, which combines
    it with the worktree-level active-delivery guards so an irreversible reaper
    never protects LESS than the reversible idle-stack reaper.
    """
    if Session.objects.filter(ticket=ticket, ended_at__isnull=True).exists():
        return True
    return Task.objects.filter(ticket=ticket, status__in=_ACTIVE_TASK_STATES).exists()


def _is_reaper_pinned(worktree: Worktree) -> bool:
    """True iff the worktree carries the explicit ``reaper_pinned`` marker (#2227)."""
    return bool(validated_worktree_extra(worktree.extra).get("reaper_pinned"))


def _has_recent_e2e_run(worktree: Worktree, *, e2e_cutoff: datetime) -> bool:
    """True iff an E2E/evidence run touched this worktree within the recent window (#2227)."""
    last = worktree.last_e2e_run
    return last is not None and last > e2e_cutoff


def _structural_keep_reason(worktree: Worktree, *, cutoff: datetime, active_path: Path | None) -> str | None:
    """The pre-#2227 reapability guards: not-running, never-started, recent, busy, CWD."""
    if not can_proceed(worktree.stop_services):
        return f"not in a running state (state={worktree.state})"
    if worktree.last_used_at is None:
        return "never started (last_used_at is null) — cannot confirm idle"
    if worktree.last_used_at > cutoff:
        return "recently used (within the idle window)"
    if ticket_is_busy(worktree.ticket):
        return "ticket has a live session or active/claimed task"
    if _is_currently_active(worktree, active_path):
        return "the currently-active worktree (CWD)"
    return None


def _active_delivery_keep_reason(worktree: Worktree, *, e2e_cutoff: datetime) -> str | None:
    """The #2227 guards: a stack under active delivery / fresh evidence / a pin is KEPT."""
    if under_external_delivery(worktree.ticket):
        return "ticket carries a live external-delivery lease"
    if _has_recent_e2e_run(worktree, e2e_cutoff=e2e_cutoff):
        return "a recent E2E/evidence run touched it"
    if _is_reaper_pinned(worktree):
        return "explicitly pinned (extra['reaper_pinned'])"
    return None


def active_delivery_keep_reason(worktree: Worktree, *, now: datetime | None = None) -> str | None:
    """The #2227 active-delivery half of the reap guard, with the e2e cutoff resolved.

    The worktree-level guards — a live external-delivery lease, a recent
    E2E/evidence run, or an explicit ``extra['reaper_pinned']`` — shared by every
    reap path. The public, self-contained wrapper over
    :func:`_active_delivery_keep_reason` (it resolves the e2e recency cutoff from
    settings). Reusable on its own: :func:`worktree_protects_against_reap` combines
    it with :func:`ticket_is_busy`, and the FSM-done worktree reaper
    (:func:`teatree.core.cleanup_liveness.worktree_liveness`) folds it in
    UNCONDITIONALLY — unlike busy-ticket / recent-commit it is NOT an FSM-ceremony
    false positive (the merge mints neither a delivery lease, an e2e run, nor a
    pin), so a worktree delivering externally / freshly e2e-tested / pinned is KEPT
    through the post-merge teardown too.
    """
    e2e_minutes = get_effective_settings().idle_stack_e2e_recent_minutes
    e2e_cutoff = (now or timezone.now()) - timedelta(minutes=e2e_minutes)
    return _active_delivery_keep_reason(worktree, e2e_cutoff=e2e_cutoff)


def worktree_protects_against_reap(worktree: Worktree, *, now: datetime | None = None) -> str | None:
    """The reason a destructive reaper must KEEP *worktree*, or ``None`` when it may reap.

    The shared liveness predicate every OPPORTUNISTIC destructive reaper/teardown
    path consults before deleting filesystem or DB state — the FSM-done worktree
    reaper (:func:`teatree.core.worktree_done.reap_done_worktree`, via
    :func:`teatree.core.cleanup_liveness.worktree_liveness`), the clean-merged
    sweep, the merge-sync cleanup, and the orphan-isolated-root reaper all route
    through it (or through :func:`teatree.core.cleanup.cleanup_worktree`). It
    combines the ticket-level :func:`ticket_is_busy` (live session /
    active-or-claimed task) with the worktree-level #2227 active-delivery guards
    (:func:`active_delivery_keep_reason` — external-delivery lease, recent E2E,
    ``reaper_pinned``) so the IRREVERSIBLE teardown reapers never protect LESS
    than the REVERSIBLE idle-stack reaper. Explicit/FSM-driven teardown bypasses
    this (it has decided to tear the worktree down); opportunistic reaps respect
    it (#291/#2243 data-loss discipline).
    """
    if ticket_is_busy(worktree.ticket):
        return "ticket has a live session or active/claimed task"
    return active_delivery_keep_reason(worktree, now=now)


def preserve_reason(
    worktree: Worktree,
    *,
    cutoff: datetime,
    e2e_cutoff: datetime,
    active_path: Path | None,
) -> str | None:
    """Return why *worktree* is KEPT by the reaper, or ``None`` when it is reapable.

    The single fail-safe predicate: the structural guards first
    (:func:`_structural_keep_reason`), then the #2227 active-delivery guards
    (:func:`_active_delivery_keep_reason`). A non-``None`` reason is a
    human-readable phrase the reaper logs so a preserve (and, by absence, a
    reap) is never silent.
    """
    structural = _structural_keep_reason(worktree, cutoff=cutoff, active_path=active_path)
    if structural is not None:
        return structural
    delivery = _active_delivery_keep_reason(worktree, e2e_cutoff=e2e_cutoff)
    if delivery is not None:
        return delivery
    running = _running_container_count(compose_project(worktree))
    if running == 0:
        logger.info("idle_stack: worktree %s has zero running containers — reaping (idempotent)", worktree.repo_path)
    return None


def classify_running_worktrees(
    *, overlay: str, idle_minutes: int, e2e_recent_minutes: int | None = None, now: datetime | None = None
) -> Iterator[tuple[Worktree, str | None]]:
    """Yield ``(worktree, preserve_reason)`` for every running worktree of *overlay* (#2227).

    The classification the reaper's tick log reads: a ``None`` reason means the
    worktree is reapable; a non-``None`` reason names why it is KEPT, so a reap
    is never silent. ``e2e_recent_minutes`` defaults to
    ``idle_stack_e2e_recent_minutes`` from config; caller-supplied *now* is the
    test/clock seam.
    """
    moment = now or timezone.now()
    cutoff = moment - timedelta(minutes=idle_minutes)
    if e2e_recent_minutes is None:
        e2e_recent_minutes = get_effective_settings().idle_stack_e2e_recent_minutes
    e2e_cutoff = moment - timedelta(minutes=e2e_recent_minutes)
    active_path = _active_worktree_path()
    candidates = (
        Worktree.objects.filter(overlay=overlay, state__in=_RUNNING_STATES)
        .filter(Q(last_used_at__isnull=False))
        .select_related("ticket")
        .order_by("pk")
    )
    for worktree in candidates:
        yield worktree, preserve_reason(worktree, cutoff=cutoff, e2e_cutoff=e2e_cutoff, active_path=active_path)


def reapable_worktrees(
    *, overlay: str, idle_minutes: int, e2e_recent_minutes: int | None = None, now: datetime | None = None
) -> Iterator[Worktree]:
    """Yield the idle running worktrees of *overlay* that should be reaped.

    Scoped per overlay (mirroring ``check_local_stack_limit``). The
    ``preserve_reason is None`` filter over :func:`classify_running_worktrees`:
    a worktree is yielded only when no structural guard and none of the #2227
    active-delivery guards keeps it.
    """
    for worktree, reason in classify_running_worktrees(
        overlay=overlay, idle_minutes=idle_minutes, e2e_recent_minutes=e2e_recent_minutes, now=now
    ):
        if reason is None:
            yield worktree


__all__ = [
    "active_delivery_keep_reason",
    "classify_running_worktrees",
    "preserve_reason",
    "reapable_worktrees",
    "ticket_is_busy",
    "worktree_protects_against_reap",
]
