"""Refuse a writing task when its checkout cannot be proven to be its own."""

import logging
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

from teatree.core.cleanup.process_table import ProcessTable, read_process_table
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.phase_tools import MCP_WRITE, tools_for_phase
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.models.checkout_custody import Custody, Tenure
from teatree.core.models.checkout_exit import mark_verified_checkout_run
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.models.usage_window_state import CHECKOUT_PARKED_PREFIX
from teatree.core.models.worktree_occupancy import (
    OccupancyHolder,
    WorktreeOccupiedError,
    _worktree_at,
    acquire,
    checkout_capacity_opened,
    drop_refused_tenure,
    occupancy_holder,
    release,
    task_holder_id,
)
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.worktree.checkout_attest import CheckoutAttestation, ask_owner_to_attest, checkout_rows
from teatree.core.worktree.checkout_recovery import attest_deferred_exit, reattest_on_proof
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.worktree_paths import paths_match

logger = logging.getLogger(__name__)
_PARK_RETRY = timedelta(minutes=30)


class CheckoutFenceError(RuntimeError):
    def __init__(self, message: str, *, attestable: CheckoutAttestation | None = None) -> None:
        super().__init__(message)
        self.attestable = attestable


def _confirm_owner_attestation(task: Task, path: str, state: CheckoutState) -> None:
    try:
        confirmed = checkout_state(Path(path), ticket=task.ticket)
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
    others = (row.worktree_path for row in Worktree.objects.filter(ticket=task.ticket).only("extra"))
    try:
        for candidate in [path, *(other for other in others if other and not paths_match(other, path))]:
            _verify_one_checkout(task, candidate)
    except CheckoutFenceError:
        held = _worktree_at(task.ticket, path)
        if held is not None:
            drop_refused_tenure(held, holder=task_holder_id(task), holder_session=task.claimed_by_session)
        raise
    mark_verified_checkout_run()


def _verify_one_checkout(task: Task, path: str) -> None:
    rows = checkout_rows(path)
    if len(rows) != 1 or not Worktree.objects.filter(pk=rows[0].pk, ticket=task.ticket).exists():
        message = f"checkout {path} needs exactly one Worktree row; found {len(rows)}"
        raise CheckoutFenceError(message)
    table = read_process_table(exclude_ancestors=True)
    if table.usable and table.holds(Path(path)):
        message = f"checkout {path} has a live process working in it"
        raise CheckoutFenceError(message)
    try:
        state = checkout_state(Path(path), ticket=task.ticket)
    except (OSError, RuntimeError, ValueError) as exc:
        message = f"checkout {path} state could not be read: {exc}"
        raise CheckoutFenceError(message) from exc
    worktree = rows[0]
    attested = worktree.get_extra().get("attested_state")
    owner_attested = f"owner:{state.digest}"
    if not table.usable and attested != owner_attested:
        message = f"checkout {path} process table unavailable: {table.refuse_reason()}"
        raise CheckoutFenceError(message, attestable=CheckoutAttestation(worktree, state, host_evidence=False))
    if attested == owner_attested:
        _confirm_owner_attestation(task, path, state)
        worktree.merge_extra(set_keys={"attested_state": state.digest})
        attested = state.digest
    if attested is None and state.pristine:
        worktree.merge_extra(set_keys={"attested_state": state.digest})
        attested = state.digest
    _accept_changed_checkout(task, worktree, state, table, attested=attested)
    _anchor_verified_checkout(task, worktree, state)


def _accept_changed_checkout(
    task: Task, worktree: Worktree, state: CheckoutState, table: ProcessTable, *, attested: str | None
) -> None:
    if attested != state.digest and not (
        attest_deferred_exit(task, worktree, state, table) or reattest_on_proof(task, worktree, state, table)
    ):
        if attested is None:
            message = (
                f"checkout {worktree.worktree_path} has no attested state; inspect it and run "
                f"`t3 <overlay> worktree attest {worktree.worktree_path}`"
            )
        else:
            message = f"checkout {worktree.worktree_path} differs from its attested state"
        raise CheckoutFenceError(
            message, attestable=CheckoutAttestation(worktree, state, host_evidence=not table.eviction_gaps)
        )


def _anchor_verified_checkout(task: Task, worktree: Worktree, state: CheckoutState) -> None:
    current = Custody.from_extra(worktree.get_extra())
    holder = task_holder_id(task)
    if (worktree.occupied_by, worktree.occupied_by_session) == (holder, task.claimed_by_session):
        if current is None or not current.tenures or current.tenures[-1].ended:
            since = worktree.occupied_at.isoformat() if worktree.occupied_at else timezone.now().isoformat()
            anchored = Custody(state.digest, (Tenure(holder, task.claimed_by_session, since),))
        else:
            anchored = Custody.reanchored(state.digest, current, holder, task.claimed_by_session)
    else:
        anchored = Custody(state.digest, ())
    worktree.merge_extra(set_keys={"checkout_custody": anchored.as_dict()})


def park_task_checkout(
    task: Task,
    *,
    reason: str,
    behind: OccupancyHolder | None = None,
    attestable: CheckoutAttestation | None = None,
) -> TaskAttempt:
    prior = TaskAttempt.objects.filter(task__ticket=task.ticket).order_by("-pk").first()
    previous = prior.result if prior is not None and isinstance(prior.result, dict) else {}
    episode = (
        previous.get("checkout_episode")
        if str(previous.get("summary", "")).startswith(CHECKOUT_PARKED_PREFIX)
        else None
    )
    blocker = {"occupied_by": {"holder": behind.holder, "session": behind.holder_session}} if behind is not None else {}
    attempt = TaskAttempt.objects.create(  # no-usage: checkout refused before a harness turn was billed
        task=task,
        ended_at=timezone.now(),
        exit_code=0,
        error=f"{CHECKOUT_PARKED_PREFIX}{reason}",
        result={
            "summary": f"{CHECKOUT_PARKED_PREFIX}{reason}",
            "checkout_episode": episode or f"attempt:{task.pk}",
            **blocker,
            **({"checkout_worktree_id": attestable.worktree.pk} if attestable is not None else {}),
        },
    )
    result = {**attempt.result, **blocker}
    if behind is None:
        result.pop("occupied_by", None)
    if episode is None:
        result["checkout_episode"] = f"attempt:{attempt.pk}"
    if result != attempt.result:
        TaskAttempt.objects.filter(pk=attempt.pk).update(result=result)
        attempt.result = result
    own_row = Task.objects.filter(pk=task.pk)
    seat_freed = bool(own_row.cheap_lane_occupancy() or own_row.expensive_lane_occupancy())
    holders = (occupancy_holder(row) for row in Worktree.objects.filter(ticket=task.ticket).exclude(occupied_by=""))
    task.park(not_before=min([timezone.now() + _PARK_RETRY, *(held.expires_at for held in holders if held)]))
    rearmed = 0
    if behind is not None and not _still_held_by(task, behind):
        rearmed = Task.objects.filter(pk=task.pk).rearm_parked_behind(
            holder=behind.holder, holder_session=behind.holder_session
        )
    if seat_freed or rearmed:
        checkout_capacity_opened.send(sender=Task)
    if attestable is not None:
        question = ask_owner_to_attest(task, attestable)
        if question is not None:
            return attempt
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


def _still_held_by(task: Task, behind: OccupancyHolder) -> bool:
    return Worktree.objects.filter(
        ticket=task.ticket,
        occupied_by=behind.holder,
        occupied_by_session=behind.holder_session,
        occupancy_expires_at__gt=timezone.now(),
    ).exists()


def park_refused_checkout(task: Task, refusal: WorktreeOccupiedError | CheckoutFenceError) -> TaskAttempt:
    behind = refusal.holder if isinstance(refusal, WorktreeOccupiedError) else None
    attestable = refusal.attestable if isinstance(refusal, CheckoutFenceError) else None
    return park_task_checkout(task, reason=str(refusal), behind=behind, attestable=attestable)


def claim_task_checkout_or_park(task: Task, *, lease_seconds: int | None = None) -> TaskAttempt | None:
    """Hold the claim's checkout for every phase, as the worker does, and fence a writing one."""
    path = dispatch_worktree_path(task.ticket)
    if not path:
        return None
    worktree = _worktree_at(task.ticket, path)
    if worktree is None:
        return park_task_checkout(task, reason=f"checkout {path} has no Worktree row")
    holder = task_holder_id(task)
    session = task.claimed_by_session
    try:
        acquire(worktree, holder=holder, holder_session=session, lease_seconds=lease_seconds)
    except WorktreeOccupiedError as exc:
        return park_refused_checkout(task, exc)
    try:
        verify_task_checkout(task, path)
    except CheckoutFenceError as exc:
        release(worktree, holder=holder, holder_session=session)
        return park_refused_checkout(task, exc)
    return None
