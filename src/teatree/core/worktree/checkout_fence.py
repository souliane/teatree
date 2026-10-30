"""Refuse a writing task when its checkout cannot be proven to be its own."""

from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from teatree.core.cleanup.process_table import ProcessTable, read_process_table
from teatree.core.factory.stuck_ticket_halt import record_ticket_halt
from teatree.core.modelkit.phase_tools import phase_writes_checkout
from teatree.core.modelkit.phases import phase_cost
from teatree.core.models import LocalStackQueueItem, Task, TaskAttempt, Worktree, WorktreeEnvOverride
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
from teatree.core.worktree.checkout_attest import checkout_rows
from teatree.core.worktree.checkout_recovery import attest_deferred_exit, reattest_on_proof
from teatree.core.worktree.checkout_released import reattest_released_pr_head
from teatree.core.worktree.checkout_safety import (
    CheckoutSafetyEvidence,
    assess_checkout_safety,
    auto_attest_safe_checkout,
)
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.worktree_paths import paths_match

_PARK_RETRY = timedelta(minutes=30)
_REPEATED_PARK_THRESHOLD = 3
_DUPLICATE_COUNT = 2


class CheckoutFenceError(RuntimeError):
    pass


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
    return phase_writes_checkout(task.phase)


def verify_task_checkout(task: Task, path: str, *, namespace: str = "") -> None:
    if not path or not writes_checkout(task):
        return
    others = (row.worktree_path for row in Worktree.objects.filter(ticket=task.ticket).only("extra"))
    try:
        for candidate in [path, *(other for other in others if other and not paths_match(other, path))]:
            _verify_one_checkout(task, candidate, namespace=namespace)
    except CheckoutFenceError:
        held = _worktree_at(task.ticket, path)
        if held is not None:
            drop_refused_tenure(held, holder=task_holder_id(task), holder_session=task.claimed_by_session)
        raise
    mark_verified_checkout_run()


def _verify_one_checkout(task: Task, path: str, *, namespace: str = "") -> None:
    rows = _release_redundant_checkout_row(task, path, checkout_rows(path))
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
    if attested == owner_attested:
        _confirm_owner_attestation(task, path, state)
        worktree.merge_extra(set_keys={"attested_state": state.digest})
    _accept_changed_checkout(task, worktree, state, table, attested)
    _anchor_verified_checkout(task, worktree, state, namespace=namespace)


def _release_redundant_checkout_row(task: Task, path: str, rows: list[Worktree]) -> list[Worktree]:
    if len(rows) != _DUPLICATE_COUNT:
        return rows
    with transaction.atomic():
        live = list(Worktree.objects.select_for_update().filter(pk__in=[row.pk for row in rows]).order_by("pk"))
        if len(live) != _DUPLICATE_COUNT or any(
            not row.worktree_path or not paths_match(row.worktree_path, path) for row in live
        ):
            return rows
        first, second = live
        if any(
            (row.ticket_id, row.overlay, row.repo_path, row.branch)
            != (task.ticket.pk, first.overlay, first.repo_path, first.branch)
            for row in live
        ):
            return rows
        duplicate = first if len(first.extra or {}) < len(second.extra or {}) else second
        if not _is_empty_duplicate(duplicate):
            return rows
        duplicate.merge_extra(pop_keys=("worktree_path",))
    return checkout_rows(path)


def _is_empty_duplicate(row: Worktree) -> bool:
    return all(
        (
            row.state in {Worktree.State.CREATED, Worktree.State.PROVISIONED},
            not row.db_name,
            not row.compose_project,
            row.last_used_at is None,
            row.last_e2e_run is None,
            not row.occupied_by,
            not row.occupied_by_session,
            not WorktreeEnvOverride.objects.filter(worktree=row).exists(),
            not LocalStackQueueItem.objects.filter(worktree=row).exists(),
            set(row.extra or {}) == {"worktree_path"},
        )
    )


def _reattest_changed_checkout(
    task: Task, worktree: Worktree, state: CheckoutState, table: ProcessTable, evidence: CheckoutSafetyEvidence
) -> bool:
    if evidence.preserved.requires_pr:
        return reattest_released_pr_head(task, worktree, state, table)
    return (
        attest_deferred_exit(task, worktree, state, table)
        or reattest_on_proof(task, worktree, state, table)
        or reattest_released_pr_head(task, worktree, state, table)
    )


def _accept_changed_checkout(
    task: Task,
    worktree: Worktree,
    state: CheckoutState,
    table: ProcessTable,
    attested: str | None,
) -> None:
    if attested == f"owner:{state.digest}":
        return
    evidence = assess_checkout_safety(task, worktree, state, table, attested=attested)
    if isinstance(evidence, str):
        raise CheckoutFenceError(evidence)
    if state.uncommitted:
        if attested == state.digest:
            return
        if attest_deferred_exit(task, worktree, state, table):
            return
        if evidence.recover_owned_dirt and reattest_on_proof(task, worktree, state, table):
            return
        reason = f"checkout {worktree.worktree_path} holds {state.uncommitted} uncommitted file(s)"
        raise CheckoutFenceError(reason)
    needs_recovery = (
        attested != state.digest or not table.usable or table.eviction_gaps or evidence.preserved.requires_pr
    )
    if (
        needs_recovery
        and not _reattest_changed_checkout(task, worktree, state, table, evidence)
        and (reason := auto_attest_safe_checkout(task, worktree, state, evidence))
    ):
        raise CheckoutFenceError(reason)


def _anchor_verified_checkout(task: Task, worktree: Worktree, state: CheckoutState, *, namespace: str = "") -> None:
    current = Custody.from_extra(worktree.get_extra())
    holder = task_holder_id(task)
    if (worktree.occupied_by, worktree.occupied_by_session) == (holder, task.claimed_by_session):
        if current is None or not current.tenures or current.tenures[-1].ended:
            since = worktree.occupied_at.isoformat() if worktree.occupied_at else timezone.now().isoformat()
            anchored = Custody(state.digest, (Tenure(holder, task.claimed_by_session, since, namespace=namespace),))
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
    unknown_holder: bool = False,
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
    seat_freed = bool(own_row.lane_occupancy(phase_cost(task.phase)))
    holders = (occupancy_holder(row) for row in Worktree.objects.filter(ticket=task.ticket).exclude(occupied_by=""))
    own_row.update(admitted_at=None)
    task.admitted_at = None
    repeats = attempt.park_repeats + 1
    delay = min(_PARK_RETRY * 2 ** min(max(repeats - 2, 0), 6), timedelta(hours=24))
    task.park(not_before=min([timezone.now() + delay, *(held.expires_at for held in holders if held)]))
    rearmed = _rearm_if_occupancy_cleared(task, behind=behind, unknown_holder=unknown_holder)
    halt_reason = f"checkout parked: {reason}"
    stamp = (task.ticket.extra or {}).get("stuck_halt")
    if (
        isinstance(stamp, dict)
        and str(stamp.get("reason", "")).startswith("checkout parked: ")
        and (rearmed or stamp.get("reason") != halt_reason)
    ):
        task.ticket.merge_extra(pop_keys=["stuck_halt"])
    if repeats >= _REPEATED_PARK_THRESHOLD and behind is None and not rearmed:
        record_ticket_halt(task.ticket, reason=halt_reason)
    if seat_freed or rearmed:
        checkout_capacity_opened.send(sender=Task)
    if rearmed:
        return attempt
    return attempt


def _still_held_by(task: Task, behind: OccupancyHolder) -> bool:
    return Worktree.objects.filter(
        ticket=task.ticket,
        occupied_by=behind.holder,
        occupied_by_session=behind.holder_session,
        occupancy_expires_at__gt=timezone.now(),
    ).exists()


def _rearm_if_occupancy_cleared(task: Task, *, behind: OccupancyHolder | None, unknown_holder: bool) -> int:
    if behind is not None:
        if _still_held_by(task, behind):
            return 0
        return Task.objects.filter(pk=task.pk).rearm_parked_behind(
            holder=behind.holder, holder_session=behind.holder_session
        )
    if not unknown_holder or any(
        occupancy_holder(row) is not None for row in Worktree.objects.filter(ticket=task.ticket).exclude(occupied_by="")
    ):
        return 0
    now = timezone.now()
    return Task.objects.filter(pk=task.pk, status=Task.Status.PENDING, not_before__gt=now).update(
        not_before=now, admitted_at=None
    )


def park_refused_checkout(task: Task, refusal: WorktreeOccupiedError | CheckoutFenceError) -> TaskAttempt:
    behind = refusal.holder if isinstance(refusal, WorktreeOccupiedError) else None
    unknown_holder = isinstance(refusal, WorktreeOccupiedError) and behind is None
    return park_task_checkout(task, reason=str(refusal), behind=behind, unknown_holder=unknown_holder)


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
