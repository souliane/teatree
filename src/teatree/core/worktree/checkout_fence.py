"""Refuse a writing task when its checkout cannot be proven to be its own."""

import logging
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

from teatree.core.cleanup.process_table import read_process_table
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.phase_tools import MCP_WRITE, tools_for_phase
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.core.models.worktree_occupancy import WorktreeOccupiedError, _worktree_at, acquire, release, task_holder_id
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.worktree_paths import paths_match

logger = logging.getLogger(__name__)
CHECKOUT_PARKED_PREFIX = "checkout_parked: "


class CheckoutFenceError(RuntimeError):
    pass


def _confirm_owner_attestation(path: str, state: CheckoutState) -> None:
    try:
        confirmed = checkout_state(Path(path))
    except (OSError, RuntimeError, ValueError) as exc:
        message = f"checkout {path} state could not be confirmed: {exc}"
        raise CheckoutFenceError(message) from exc
    if confirmed.digest != state.digest:
        message = f"checkout {path} changed while consuming owner attestation"
        raise CheckoutFenceError(message)


def writes_checkout(task: Task) -> bool:
    return bool(tools_for_phase(task.phase) & {"write_file", "edit_file", MCP_WRITE})


def verify_task_checkout(task: Task, path: str) -> None:
    if not path or not writes_checkout(task):
        return
    rows = [
        row
        for row in Worktree.objects.exclude(extra__worktree_path__isnull=True).only("ticket_id", "extra")
        if row.worktree_path and paths_match(row.worktree_path, path)
    ]
    if len(rows) != 1 or rows[0].ticket_id != task.ticket.pk:
        message = f"checkout {path} needs exactly one Worktree row; found {len(rows)}"
        raise CheckoutFenceError(message)
    table = read_process_table()
    if table.usable and table.holds(Path(path)):
        message = f"checkout {path} has a live process working in it"
        raise CheckoutFenceError(message)
    try:
        state = checkout_state(Path(path))
    except (OSError, RuntimeError, ValueError) as exc:
        message = f"checkout {path} state could not be read: {exc}"
        raise CheckoutFenceError(message) from exc
    worktree = rows[0]
    attested = worktree.get_extra().get("attested_state")
    owner_attested = f"owner:{state.digest}"
    if not table.usable and attested != owner_attested:
        message = f"checkout {path} process table unavailable: {table.refuse_reason()}"
        raise CheckoutFenceError(message)
    if attested == owner_attested:
        _confirm_owner_attestation(path, state)
        worktree.merge_extra(set_keys={"attested_state": state.digest})
        attested = state.digest
    if attested is None and state.pristine:
        worktree.merge_extra(set_keys={"attested_state": state.digest})
        attested = state.digest
    if attested != state.digest:
        message = f"checkout {path} differs from its attested state"
        raise CheckoutFenceError(message)


def park_task_checkout(task: Task, *, reason: str) -> TaskAttempt:
    prior = TaskAttempt.objects.filter(task__ticket=task.ticket).order_by("-pk").first()
    previous = prior.result if prior is not None and isinstance(prior.result, dict) else {}
    episode = (
        previous.get("checkout_episode")
        if str(previous.get("summary", "")).startswith(CHECKOUT_PARKED_PREFIX)
        else None
    )
    attempt = TaskAttempt.objects.create(  # no-usage: checkout refused before a harness turn was billed
        task=task,
        ended_at=timezone.now(),
        exit_code=0,
        error=f"{LIMIT_PARKED_PREFIX}{CHECKOUT_PARKED_PREFIX}{reason}",
        result={"summary": f"{CHECKOUT_PARKED_PREFIX}{reason}", "checkout_episode": episode or f"attempt:{task.pk}"},
    )
    if episode is None:
        result = dict(attempt.result)
        result["checkout_episode"] = f"attempt:{attempt.pk}"
        TaskAttempt.objects.filter(pk=attempt.pk).update(result=result)
        attempt.result = result
    task.park(not_before=timezone.now() + timedelta(seconds=30))
    try:
        notify_user(
            f"Ticket {task.ticket.pk} is parked before writing its checkout: {reason}",
            kind=NotifyKind.INFO,
            idempotency_key=f"checkout-hold:{task.ticket.pk}:{attempt.result['checkout_episode']}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.exception("Could not page owner for checkout hold on task %s", task.pk)
    return attempt


def claim_task_checkout_or_park(task: Task, *, lease_seconds: int | None = None) -> TaskAttempt | None:
    path = dispatch_worktree_path(task.ticket)
    if not path or not writes_checkout(task):
        return None
    worktree = _worktree_at(task.ticket, path)
    if worktree is None:
        return park_task_checkout(task, reason=f"checkout {path} has no Worktree row")
    holder = task_holder_id(task)
    session = task.claimed_by_session
    try:
        acquire(worktree, holder=holder, holder_session=session, lease_seconds=lease_seconds)
    except WorktreeOccupiedError as exc:
        return park_task_checkout(task, reason=str(exc))
    try:
        verify_task_checkout(task, path)
    except CheckoutFenceError as exc:
        release(worktree, holder=holder, holder_session=session)
        return park_task_checkout(task, reason=str(exc))
    return None
