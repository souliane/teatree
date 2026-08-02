"""Idle-stack detection for the idle-stack reaper (souliane/teatree#2190, #2227).

A locally-running worktree (``SERVICES_UP`` / ``READY``) holds a docker stack,
language servers, browsers and CI processes — and a
``max_concurrent_local_stacks`` slot. When that worktree's ticket is IDLE, the
stack needlessly holds RAM and the slot, stalling new work. The idle-stack
reaper stops such a stack (``Worktree.stop_services`` → demote to
``PROVISIONED``, REVERSIBLE — DB + worktree preserved) to free the slot.

A worktree is REAPABLE when ALL hold: (1) its state is ``SERVICES_UP`` or
``READY`` — a dormant ``PROVISIONED`` row holds no stack, so it is never a
candidate; (2) its ticket has NO live :class:`Session` (open AND active within
``session_stale_after_hours``) and NO active/claimed :class:`Task` (``PENDING``
/ ``CLAIMED``); (3)
``last_used_at`` is older than the idle threshold — a null ``last_used_at``
cannot be confirmed idle, so it is a fail-safe KEEP; (4) it is not the
currently-active worktree (the process CWD lives inside it); (5) #2227 — its
ticket carries NO live external-delivery lease (the same lease that gates
dispatch), it has NOT been touched by an E2E/evidence run within
``idle_stack_e2e_recent_minutes`` (``Worktree.last_e2e_run``), and it is NOT
explicitly pinned (``extra['reaper_pinned']``). A stack under active delivery
or holding fresh evidence is the live target of in-flight work — reaping it
forces a slow re-provision; and (6) its own docker stack is PROVEN QUIET
(:func:`stack_activity`).

Guards (2)-(5) are all authored by the CONTROL PLANE: an FSM transition, a
``Session``/``Task`` row, a lease, a post-hoc ``lifecycle record-e2e-run``.
None of them can see a stack being driven OUT OF BAND — a Playwright run, a
browser, a ``curl`` — because such a driver writes no row anywhere the reaper
reads (it may not even share the reaper's database). Inferring idleness from
control-plane silence is therefore FAIL-OPEN: absence of a teatree-authored
signal is the reaper's own blindness, not proof that nobody is using the
stack. Guard (6) closes that: it asks the STACK, over the docker socket the
reaper already holds, whether it has emitted anything inside the window — an
HTTP request served for an out-of-band driver logs in the app tier within
seconds, so a live E2E run registers by construction and needs no cooperation
from the runner. It keys on the compose project name, never on
``worktree_path``, so it answers identically from a container whose mounts
resolve that path differently.

FAIL-SAFE doctrine: every uncertainty resolves to KEEP — including a docker
probe that cannot answer (``StackActivity.UNKNOWN``). A db-only partial stack
(the wt595 leak class — app tier down but a stray ``db-1`` lingering) is
reapable once quiet, not "healthy": ``stop_services`` brings the WHOLE compose
project down so no stray container survives.

:func:`preserve_reason` is the single predicate: it returns the human-readable
reason a worktree is KEPT, or ``None`` when it is reapable.
:func:`classify_running_worktrees` yields ``(worktree, reason)`` for every
running candidate so the reaper's tick log can surface preserved-vs-reaped — a
reap is never silent. :func:`reapable_worktrees` is the ``reason is None``
filter over that classification.
"""

import logging
import subprocess  # noqa: S404 — imported only for the SubprocessError type caught below; shell-outs go through teatree.utils.run
from collections.abc import Iterator
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from django.db.models import Q
from django.utils import timezone
from django_fsm import can_proceed

from teatree.config import get_effective_settings
from teatree.core.models import Ticket, Worktree
from teatree.core.models.external_delivery import under_external_delivery
from teatree.core.models.types import validated_worktree_extra
from teatree.core.worktree.worktree_env import compose_project
from teatree.core.worktree.worktree_paths import paths_match
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

_RUNNING_STATES: tuple[str, ...] = (Worktree.State.SERVICES_UP, Worktree.State.READY)

_DOCKER_PROBE_TIMEOUT_SECONDS = 10.0


class StackActivity(StrEnum):
    """Whether a compose project's own containers prove it busy, quiet, or neither."""

    BUSY = "busy"
    QUIET = "quiet"
    UNKNOWN = "unknown"


def _docker_probe(cmd: list[str]) -> str | None:
    """The combined output of a read-only docker probe, or ``None`` when docker could not answer.

    A missing binary, an unreachable daemon socket, a wedged daemon (timeout)
    and a non-zero exit all collapse to ``None`` — the caller turns that into
    ``StackActivity.UNKNOWN`` so an unanswerable probe KEEPS the stack rather
    than crashing the tick or silently reading as "quiet". Both streams are
    joined because ``docker logs`` forwards the container's stdout and stderr
    separately and a dev app server logs its requests on stderr.
    """
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, timeout=_DOCKER_PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout + result.stderr if result.returncode == 0 else None


def _project_container_ids(project: str) -> list[str] | None:
    """The running container ids of *project*, or ``None`` when docker could not answer."""
    output = _docker_probe(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.ID}}"]
    )
    if output is None:
        return None
    return [line.strip() for line in output.splitlines() if line.strip()]


def _emitted_within_window(container_id: str, *, window_minutes: int) -> bool | None:
    """Whether *container_id* wrote any log line inside the window; ``None`` if unanswerable.

    The window is passed as a RELATIVE duration so the daemon resolves it
    against its own clock — host/container clock skew cannot make a busy stack
    read quiet.
    """
    output = _docker_probe(["docker", "logs", "--since", f"{window_minutes}m", "--tail", "1", container_id])
    if output is None:
        return None
    return bool(output.strip())


def stack_activity(project: str, *, window_minutes: int) -> StackActivity:
    """Ask the STACK — not the control plane — whether anything is driving it.

    ``BUSY`` when any container of *project* emitted output inside the window:
    an out-of-band driver (Playwright, a browser, ``curl``) leaves no row the
    reaper can read, but the HTTP request it serves is logged by the app tier
    within seconds, so a live run registers here by construction. ``QUIET``
    only when EVERY container was proven silent — or when the project has no
    running container at all, which nobody can be driving (stopping it is then
    idempotent bookkeeping). ``UNKNOWN`` for an unnamed project or any probe
    docker could not answer.
    """
    if not project:
        return StackActivity.UNKNOWN
    container_ids = _project_container_ids(project)
    if container_ids is None:
        return StackActivity.UNKNOWN
    if not container_ids:
        return StackActivity.QUIET
    window = max(1, window_minutes)
    for container_id in container_ids:
        emitted = _emitted_within_window(container_id, window_minutes=window)
        if emitted is None:
            return StackActivity.UNKNOWN
        if emitted:
            return StackActivity.BUSY
    return StackActivity.QUIET


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
    return paths_match(active_path, wt_path) or Path(wt_path).resolve() in active_path.parents


def ticket_is_busy(ticket: Ticket) -> bool:
    """True iff *ticket* has a live session or an active/claimed task.

    The ticket-level half of the liveness signal, delegating to the single owner
    :meth:`teatree.core.models.ticket.Ticket.has_active_work`. Reapers do not call
    this directly — they call :func:`worktree_protects_against_reap`, which combines
    it with the worktree-level active-delivery guards so an irreversible reaper
    never protects LESS than the reversible idle-stack reaper.
    """
    return ticket.has_active_work()


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
    (:func:`teatree.core.cleanup.cleanup_liveness.worktree_liveness`) folds it in
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
    reaper (:func:`teatree.core.worktree.worktree_done.reap_done_worktree`, via
    :func:`teatree.core.cleanup.cleanup_liveness.worktree_liveness`), the clean-merged
    sweep, the merge-sync cleanup, and the orphan-isolated-root reaper all route
    through it (or through :func:`teatree.core.cleanup.cleanup.cleanup_worktree`). It
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


def _stack_activity_keep_reason(worktree: Worktree, *, window_minutes: int) -> str | None:
    """The reason the stack's OWN containers keep it, or ``None`` when proven quiet.

    The only guard that can see an out-of-band driver, and the only one whose
    "cannot tell" answer is honest rather than inferred. ``UNKNOWN`` KEEPS: a
    stack the reaper cannot prove idle is not idle.
    """
    project = compose_project(worktree)
    activity = stack_activity(project, window_minutes=window_minutes)
    if activity is StackActivity.BUSY:
        return f"its stack emitted output within the last {window_minutes}m — something is driving it"
    if activity is StackActivity.UNKNOWN:
        return f"could not prove the stack {project or '<unnamed project>'} is quiet — fail-closed KEEP"
    logger.info("idle_stack: stack %s proven quiet across every container — reaping", project or worktree.repo_path)
    return None


def preserve_reason(
    worktree: Worktree,
    *,
    cutoff: datetime,
    e2e_cutoff: datetime,
    active_path: Path | None,
    activity_window_minutes: int,
) -> str | None:
    """Return why *worktree* is KEPT by the reaper, or ``None`` when it is reapable.

    The single fail-safe predicate, cheapest guard first: the structural guards
    (:func:`_structural_keep_reason`), then the #2227 active-delivery guards
    (:func:`_active_delivery_keep_reason`), then the docker-observed
    stack-activity guard (:func:`_stack_activity_keep_reason`) — which shells
    out, so it runs only for a candidate every DB guard already cleared. A
    non-``None`` reason is a human-readable phrase the reaper logs so a
    preserve (and, by absence, a reap) is never silent.
    """
    structural = _structural_keep_reason(worktree, cutoff=cutoff, active_path=active_path)
    if structural is not None:
        return structural
    delivery = _active_delivery_keep_reason(worktree, e2e_cutoff=e2e_cutoff)
    if delivery is not None:
        return delivery
    return _stack_activity_keep_reason(worktree, window_minutes=activity_window_minutes)


def classify_running_worktrees(
    *, overlay: str, idle_minutes: int, e2e_recent_minutes: int | None = None, now: datetime | None = None
) -> Iterator[tuple[Worktree, str | None]]:
    """Yield ``(worktree, preserve_reason)`` for every running worktree of *overlay* (#2227).

    The classification the reaper's tick log reads: a ``None`` reason means the
    worktree is reapable; a non-``None`` reason names why it is KEPT, so a reap
    is never silent. ``e2e_recent_minutes`` defaults to
    ``idle_stack_e2e_recent_minutes`` from config; caller-supplied *now* is the
    test/clock seam. The docker stack-activity window reuses *idle_minutes*, so
    "idle" has ONE meaning: no control-plane touch AND no stack output for that
    many minutes.
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
        yield (
            worktree,
            preserve_reason(
                worktree,
                cutoff=cutoff,
                e2e_cutoff=e2e_cutoff,
                active_path=active_path,
                activity_window_minutes=idle_minutes,
            ),
        )


def reapable_worktrees(
    *, overlay: str, idle_minutes: int, e2e_recent_minutes: int | None = None, now: datetime | None = None
) -> Iterator[Worktree]:
    """Yield the idle running worktrees of *overlay* that should be reaped.

    Scoped per overlay (mirroring ``check_local_stack_limit``). The
    ``preserve_reason is None`` filter over :func:`classify_running_worktrees`:
    a worktree is yielded only when no structural guard, none of the #2227
    active-delivery guards, and no observed stack activity keeps it.
    """
    for worktree, reason in classify_running_worktrees(
        overlay=overlay, idle_minutes=idle_minutes, e2e_recent_minutes=e2e_recent_minutes, now=now
    ):
        if reason is None:
            yield worktree


__all__ = [
    "StackActivity",
    "active_delivery_keep_reason",
    "classify_running_worktrees",
    "preserve_reason",
    "reapable_worktrees",
    "stack_activity",
    "ticket_is_busy",
    "worktree_protects_against_reap",
]
