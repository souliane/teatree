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
"""

from collections.abc import Callable

from teatree.config import get_effective_settings
from teatree.core.models import Worktree


class LocalStackLimitExceededError(RuntimeError):
    """Refusal raised when starting a worktree would breach the per-overlay cap."""


_BLOCKING_STATES: tuple[str, ...] = (Worktree.State.SERVICES_UP, Worktree.State.READY)


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
    blockers = list(
        Worktree.objects.filter(
            overlay=candidate.overlay,
            state__in=_BLOCKING_STATES,
        )
        .exclude(ticket__pk=candidate_ticket_pk)
        .order_by("ticket__pk", "pk"),
    )
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
