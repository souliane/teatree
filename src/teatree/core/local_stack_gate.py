"""Pre-start gate for ``max_concurrent_local_stacks`` (souliane/teatree#1397).

A locally-running worktree (``SERVICES_UP`` / ``READY``) holds a docker
stack, language servers, browsers and CI processes; on a memory-
constrained host (the 2026-05-27 OOM when two stacks ran in parallel)
one stack at a time is the workable limit.

The gate is opt-in (default ``0`` = unbounded) and per-overlay scoped:
an overlay can cap to ``1`` while a cheap dogfood overlay stays
unbounded. ``check_local_stack_limit`` is called from
``t3 <overlay> worktree start`` and ``workspace start`` before the FSM
advances into ``SERVICES_UP``; refusal raises
``LocalStackLimitExceededError`` naming every blocker so the operator
knows which ``t3 <overlay> worktree teardown`` to run.

The candidate worktree's own row is excluded from the count so an
idempotent re-fire of ``start`` against an already-running worktree
does not refuse against itself.

The DB FSM state alone is not trusted: a ``SERVICES_UP`` / ``READY``
row whose docker stack is gone (a docker restart, a manual
``compose down``, an OOM kill) is a phantom that would otherwise refuse
every legitimate start forever. Before counting, each blocker is
reconciled against live ``docker ps`` — a counted row with zero running
containers is demoted to ``PROVISIONED`` (with a log line) and excluded.
A docker binary that cannot be queried fails safe: the row stays counted.
"""

import logging
from collections.abc import Callable

from teatree.config import get_effective_settings
from teatree.core.models import Worktree
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)


class LocalStackLimitExceededError(RuntimeError):
    """Refusal raised when starting a worktree would breach the per-overlay cap."""


_BLOCKING_STATES: tuple[str, ...] = (Worktree.State.SERVICES_UP, Worktree.State.READY)


def _running_container_count(compose_project: str) -> int:
    """Count *running* docker containers belonging to *compose_project*.

    ``docker ps`` (no ``-a``) lists only running containers, so a stack
    whose containers were stopped or removed (a docker restart, a manual
    ``compose down``, an OOM kill) reports zero — the signal that a
    ``SERVICES_UP`` / ``READY`` row is a phantom holding no real stack.
    A docker binary that is missing or erroring yields ``-1`` so the
    caller can distinguish "verified empty" from "could not verify" and
    fail safe (keep the row counted) on the latter.
    """
    if not compose_project:
        return -1
    result = run_allowed_to_fail(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={compose_project}",
            "--format",
            "{{.Names}}",
        ],
        expected_codes=None,
    )
    if result.returncode != 0:
        return -1
    return sum(1 for name in result.stdout.splitlines() if name.strip())


def _reconcile_phantom_blocker(worktree: Worktree) -> bool:
    """Demote *worktree* to ``PROVISIONED`` when its compose stack is gone.

    Returns ``True`` when the row is a verified phantom (state says a
    stack is up but docker reports zero running containers) and was
    demoted, so the gate must not count it. Returns ``False`` when the
    stack is genuinely live or its liveness could not be verified — both
    keep the row counted (fail safe).
    """
    ticket = worktree.ticket
    compose_project = f"{worktree.repo_path}-wt{ticket.ticket_number}" if ticket else worktree.repo_path
    if _running_container_count(compose_project) != 0:
        return False
    logger.warning(
        "Demoting phantom worktree %s (%s): state=%s but compose project %r has zero live containers",
        worktree.repo_path,
        worktree.branch,
        worktree.state,
        compose_project,
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
    a single read-through call so ``T3_OVERLAY_NAME`` and
    ``[overlays.<name>] max_concurrent_local_stacks`` overrides win
    over the global ``[teatree]`` value.
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

    candidate_ticket_pk = candidate.ticket.pk
    counted_blockers = list(
        Worktree.objects.filter(
            overlay=candidate.overlay,
            state__in=_BLOCKING_STATES,
        )
        .exclude(ticket__pk=candidate_ticket_pk)
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
        f"{candidate.overlay!r} would be exceeded by starting "
        f"{_blocker_label(candidate)}.\n"
        f"Currently running:\n  - {blocker_list}\n"
        f"Run `t3 {candidate.overlay or '<overlay>'} worktree teardown <path>` "
        "on one of the above before starting a new stack."
    )
    raise LocalStackLimitExceededError(msg)


def refuse_if_limit_exceeded(candidate: Worktree | None, *, write_err: Callable[[str], object]) -> None:
    """CLI shim around ``check_local_stack_limit``.

    Calls the gate, writes the refusal to ``write_err`` on
    :class:`LocalStackLimitExceededError`, and raises ``SystemExit(1)``.
    Accepts ``None`` (the empty-workspace case in
    ``workspace start``) and no-ops so the caller stays a one-liner.
    """
    if candidate is None:
        return
    try:
        check_local_stack_limit(candidate)
    except LocalStackLimitExceededError as exc:
        write_err(str(exc))
        raise SystemExit(1) from exc
