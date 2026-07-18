"""Pre-start gate for ``max_concurrent_local_stacks`` (souliane/teatree#1397).

A locally-running worktree (``SERVICES_UP`` / ``READY``) holds a docker
stack, language servers, browsers and CI processes; on a memory-
constrained host (the 2026-05-27 OOM when two stacks ran in parallel)
one stack at a time is the workable limit.

The gate defaults to ``1`` (a single in-flight stack, the headless-safe
cap) and is per-overlay scoped: an overlay can raise or drop the cap, and
``0`` restores unbounded behaviour for a cheap dogfood overlay.
``check_local_stack_limit`` is called from
``t3 <overlay> worktree start`` and ``workspace start`` before the FSM
advances into ``SERVICES_UP``; refusal raises
``LocalStackLimitExceededError`` naming every blocker so the operator
knows which ``t3 <overlay> worktree teardown`` to run.

The candidate worktree's own row is excluded from the count so an
idempotent re-fire of ``start`` against an already-running worktree
does not refuse against itself.

The DB FSM state alone is not trusted: a ``SERVICES_UP`` / ``READY``
row whose docker stack is gone (an OOM kill, a manual ``compose down``)
is a phantom that would otherwise refuse every legitimate start forever.
Before counting, each blocker is reconciled against docker — a row whose
compose project has zero running **and** zero existing containers is
demoted to ``PROVISIONED`` (with a log line) and excluded. A row with
zero running but existing containers is mid-restart (``docker compose
restart``, a Docker-daemon reboot), not gone: it stays counted so the
gate never corrupts a live stack's FSM during a restart window. A docker
binary that cannot be queried also fails safe: the row stays counted.
"""

import logging
from collections.abc import Callable

from django.db import transaction
from django_fsm import can_proceed

from teatree.config import get_effective_settings
from teatree.core.gates.provision_admission_gate import check_provision_admission
from teatree.core.models import Worktree
from teatree.core.worktree.worktree_env import compose_project
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)


class LocalStackLimitExceededError(RuntimeError):
    """Refusal raised when starting a worktree would breach the per-overlay cap."""


_BLOCKING_STATES: tuple[str, ...] = (Worktree.State.SERVICES_UP, Worktree.State.READY)


def _container_count(project: str, *, include_stopped: bool) -> int:
    """Count docker containers belonging to *project*.

    With *include_stopped* false (``docker ps``) only running containers
    are counted; with it true (``docker ps -a``) stopped/restarting ones
    count too. A docker binary that is missing or erroring yields ``-1``
    so the caller can distinguish "verified empty" from "could not verify"
    and fail safe (keep the row counted) on the latter.
    """
    if not project:
        return -1
    cmd = ["docker", "ps"]
    if include_stopped:
        cmd.append("-a")
    cmd += ["--filter", f"label=com.docker.compose.project={project}", "--format", "{{.Names}}"]
    result = run_allowed_to_fail(cmd, expected_codes=None)
    if result.returncode != 0:
        return -1
    return sum(1 for name in result.stdout.splitlines() if name.strip())


def _running_container_count(project: str) -> int:
    """Count *running* docker containers belonging to *project*."""
    return _container_count(project, include_stopped=False)


def _existing_container_count(project: str) -> int:
    """Count *all* docker containers (running or stopped) belonging to *project*.

    A worktree mid-restart (``docker compose restart``, a Docker-daemon
    reboot) reports zero running containers while its containers still
    exist; ``docker ps -a`` distinguishes that live-but-restarting stack
    from a genuinely-gone one.
    """
    return _container_count(project, include_stopped=True)


def _reconcile_phantom_blocker(worktree: Worktree) -> bool:
    """Demote *worktree* to ``PROVISIONED`` when its compose stack is gone.

    Returns ``True`` only when the row is a *verified-gone* phantom — zero
    running **and** zero existing containers — and was demoted, so the gate
    must not count it. A stack with zero running but existing containers is
    mid-restart, not gone: it stays counted (``False``) so the gate never
    corrupts a live stack's FSM by demoting it during a restart window. A
    stack that is genuinely live, or whose container existence could not be
    verified, also stays counted (fail safe).
    """
    project = compose_project(worktree)
    if _running_container_count(project) != 0:
        return False
    if _existing_container_count(project) != 0:
        return False
    logger.warning(
        "Demoting phantom worktree %s (%s): state=%s but compose project %r has zero containers",
        worktree.repo_path,
        worktree.branch,
        worktree.state,
        project,
    )
    worktree.state = Worktree.State.PROVISIONED
    worktree.save(update_fields=["state"])
    return True


def _blocker_label(worktree: Worktree) -> str:
    """Human-readable identifier for a blocking worktree.

    Prefers the on-disk worktree path (the argument the operator passes
    to ``t3 <overlay> worktree teardown``); falls back to the
    ``repo_path``/branch pair when the path is not yet populated.
    """
    extra = worktree.extra if isinstance(worktree.extra, dict) else {}
    path = str(extra.get("worktree_path", ""))
    if path:
        return path
    return f"{worktree.repo_path} ({worktree.branch})"


def resolve_max_concurrent_local_stacks() -> int:
    """Resolve the effective limit, applying env + per-overlay + global chain.

    Mirrors the other gates that consume ``get_effective_settings`` —
    a single read-through call so ``T3_OVERLAY_NAME`` and the per-overlay
    ``ConfigSetting`` row for the DB-home ``max_concurrent_local_stacks``
    setting win over its global-scope row. A ``[teatree]`` /
    ``[overlays.<name>]`` TOML value is ignored on read.
    """
    return int(get_effective_settings().max_concurrent_local_stacks)


def check_local_stack_limit(candidate: Worktree, *, limit: int | None = None) -> None:
    """Refuse the impending start when the overlay's cap would be exceeded.

    ``candidate`` is the worktree about to enter ``SERVICES_UP``. Every
    worktree belonging to the same ticket as the candidate is excluded
    from the count — a multi-repo ticket is *one* logical local stack,
    and an idempotent re-fire of ``start`` against an already-running
    worktree must not refuse against itself. The blocker count is then
    the number of *distinct tickets* (other than the candidate's) whose
    worktrees are currently in ``SERVICES_UP`` / ``READY``. ``limit``
    defaults to the effective config value; passing it explicitly is
    the test seam.
    """
    effective_limit = resolve_max_concurrent_local_stacks() if limit is None else limit
    if effective_limit <= 0:
        return

    # Scope blockers by the TICKET's overlay, not ``Worktree.overlay``. A row
    # auto-detected via cwd (``resolve_worktree``) can carry ``Worktree.overlay=''``
    # while its ticket carries the real overlay; counting by the ticket overlay
    # holds the cap even for such rows so an empty ``Worktree.overlay`` cannot
    # smuggle a second stack past the limit (#1397 defense-in-depth).
    candidate_ticket = candidate.ticket
    overlay = candidate_ticket.overlay or candidate.overlay
    counted_blockers = list(
        Worktree.objects.filter(
            ticket__overlay=overlay,
            state__in=_BLOCKING_STATES,
        )
        .exclude(ticket__pk=candidate_ticket.pk)
        .order_by("ticket__pk", "pk"),
    )
    blockers = [b for b in counted_blockers if not _reconcile_phantom_blocker(b)]
    blocking_tickets = {b.ticket.pk for b in blockers}
    if len(blocking_tickets) < effective_limit:
        return

    labels = [_blocker_label(b) for b in blockers]
    blocker_list = "\n  - ".join(labels)
    msg = (
        f"max_concurrent_local_stacks={effective_limit} for overlay "
        f"{overlay!r} would be exceeded by starting "
        f"{_blocker_label(candidate)}.\n"
        f"Currently running:\n  - {blocker_list}\n"
        f"Run `t3 {overlay or '<overlay>'} worktree teardown <path>` "
        "on one of the above before starting a new stack."
    )
    raise LocalStackLimitExceededError(msg)


def reap_idle_stacks(*, overlay: str, write_out: Callable[[str], object] | None = None) -> int:
    """Stop the idle running stacks of *overlay*, returning the count reaped (#2190).

    The root-cause action behind ``acquire_or_enqueue``: an idle
    ``services_up``/``ready`` worktree (no live session/task, ``last_used_at``
    past the threshold, not the active worktree) is demoted to ``provisioned``
    via ``Worktree.stop_services`` — REVERSIBLE (DB + worktree preserved). Each
    demotion is guarded by ``can_proceed`` so a stale read never raises, and
    runs in its own transaction so one failing stop does not abort the rest.
    Fail-safe: any uncertainty in ``reapable_worktrees`` keeps the stack
    running.
    """
    from teatree.core.gates.idle_stack import reapable_worktrees  # noqa: PLC0415 — deferred: call-time import

    idle_minutes = int(get_effective_settings().idle_stack_idle_minutes)
    reaped = 0
    for worktree in list(reapable_worktrees(overlay=overlay, idle_minutes=idle_minutes)):
        with transaction.atomic():
            locked = Worktree.objects.select_for_update().select_related("ticket").get(pk=worktree.pk)
            if not can_proceed(locked.stop_services):
                continue
            locked.stop_services()
            locked.save()
        reaped += 1
        if write_out is not None:
            write_out(f"  Reaped idle stack {_blocker_label(worktree)} (demoted to provisioned).")
    return reaped


def _ram_admission_holds(candidate: Worktree, *, write_out: Callable[[str], object]) -> bool:
    """Enqueue *candidate* and return ``True`` when host RAM is at/above the ceiling (#2949).

    Reuses the parallel-provision admission gate so the START path and the
    ``workspace provision`` path share one RAM ceiling. A hold enqueues to the
    existing durable queue (idempotently) — the drainer re-checks and starts it
    once RAM frees, so nothing is lost.
    """
    verdict = check_provision_admission()
    if verdict.ok:
        return False
    from teatree.core.models import LocalStackQueueItem  # noqa: PLC0415 — deferred: ORM/app-registry

    LocalStackQueueItem.objects.enqueue(candidate)
    write_out(f"  Host RAM over ceiling — queued {_blocker_label(candidate)} for retry ({verdict.reason}).")
    return True


def acquire_or_enqueue(candidate: Worktree | None, *, write_out: Callable[[str], object]) -> bool:
    """Acquire a local-stack slot for *candidate*, or enqueue when none is free (#2190, #44).

    Replaces the old ``SystemExit(1)`` refusal. The sequence: (1) check the
    cap — a free slot returns ``True`` immediately; (2) on a breach, reap idle
    stacks and re-check — a freed slot returns ``True``; (3) if still full,
    ENQUEUE a :class:`LocalStackQueueItem` (idempotently), print a "queued"
    notice, and return ``False`` so the caller does NOT advance the FSM. The
    loop's queue-drainer later re-fires ``start`` once a slot frees, with a
    Fibonacci-minute backoff. ``None`` (the empty-workspace case) acquires
    trivially.

    Returns ``True`` when the caller may proceed to ``start_services``,
    ``False`` when the request was queued (caller must stop). Never raises
    ``SystemExit`` — that is the whole point of the change.
    """
    if candidate is None:
        return True
    # #2949 resource-aware admission: on an overlay that has opted into a bounded
    # local-stack cap (the memory-constrained hosts the gate exists for), also
    # HOLD a new stack when host RAM is over the ceiling — even when a count slot
    # is free. The request is not lost: it is enqueued to the SAME durable queue
    # the count-full path uses, so the loop's drainer starts it once RAM frees.
    # An unbounded overlay (limit ``0``) is untouched — RAM is not consulted there.
    if resolve_max_concurrent_local_stacks() > 0 and _ram_admission_holds(candidate, write_out=write_out):
        return False
    try:
        check_local_stack_limit(candidate)
    except LocalStackLimitExceededError:
        pass
    else:
        return True

    reap_idle_stacks(overlay=candidate.overlay, write_out=write_out)
    try:
        check_local_stack_limit(candidate)
    except LocalStackLimitExceededError as exc:
        from teatree.core.models import LocalStackQueueItem  # noqa: PLC0415 — deferred: ORM/app-registry

        LocalStackQueueItem.objects.enqueue(candidate)
        write_out(
            f"  No free local-stack slot — queued {_blocker_label(candidate)} for retry "
            f"(the loop will start it once a slot frees).\n{exc}",
        )
        return False
    return True
