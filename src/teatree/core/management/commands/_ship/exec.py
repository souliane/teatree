"""Ship execution: the ``ship()`` FSM transition and the inline/async push (extracted from ``pr.py``).

``pr create`` validates the deterministic gates, then either *enqueues* ``execute_ship``
(default) or ships *inline* (``--sync``). The inline ship pushes and opens the PR with no
transaction open, then commits ``ship()`` + ``request_review()`` in one short block: the
SQLite write lock does not cover push or PR-open I/O, and the FSM moves only once the PR exists.
"""

import logging
from typing import TypedDict

from django.db import transaction
from django_fsm import TransitionNotAllowed

from teatree.core.models import InvalidTransitionError, Ticket
from teatree.core.runners.ship import ShipExecutor

logger = logging.getLogger(__name__)


class ShipEnqueued(TypedDict):
    ticket_id: int
    state: str
    queued: bool
    warning: str


class ShipExecuted(TypedDict):
    ticket_id: int
    state: str
    synced: bool
    ok: bool
    detail: str


class ShippingGateFailure(TypedDict):
    allowed: bool
    error: str
    missing: list[str]
    hint: str


def _gate_failure(ticket: Ticket, exc: Exception) -> ShippingGateFailure:
    ticket.refresh_from_db()
    return ShippingGateFailure(
        allowed=False,
        error=str(exc) or f"Cannot ship from state '{ticket.state}': FSM not in SELF_REVIEWED.",
        missing=[],
        hint="Drop --skip-validation so the gate can reconcile the FSM, or record the missing phases.",
    )


def _sync_result(ticket: Ticket, *, ok: bool, detail: str) -> ShipExecuted:
    ticket.refresh_from_db()
    return ShipExecuted(ticket_id=int(ticket.pk), state=str(ticket.state), synced=True, ok=ok, detail=detail)


def _do_ship_transition(ticket: Ticket, title: str) -> ShippingGateFailure | None:
    """Run the ``ship()`` FSM transition; return a gate failure or ``None``.

    Invariant (#694): ``pr create`` never raises a raw
    ``TransitionNotAllowed``. Since #748 the ``--skip-validation`` path
    runs ``reconcile_fsm_for_ship`` too (it is the user-authorized
    attestation substitute, so the FSM follows the authorization), so
    ``ship()`` is normally legal here; this ``try`` remains the backstop
    for any residual illegal hop (e.g. a state the reconcile no-ops past)
    so the failure is reported as the same structured shape the gate-fail
    path returns rather than raised. ``ship()`` schedules
    ``execute_ship.enqueue`` via ``transaction.on_commit``.

    #884: ``ship()`` also calls ``_refuse_if_worktree_dirty`` which raises
    :class:`DirtyWorktreeError` — an ``InvalidTransitionError`` (a
    ``ValueError``), NOT a django-fsm ``TransitionNotAllowed``. Both refusal
    families mean "the ship transition is not allowed right now"; both must
    surface as the same structured ``ShippingGateFailure`` contract rather
    than escape the command as an uncaught exception. The ``except`` catches
    both, using ``str(exc)`` so the dirty-worktree refusal's actionable
    message (which worktree, commit-or-discard) reaches the operator.
    """
    try:
        with transaction.atomic():
            if title:
                # #800 N3: canonical locked RMW (was an unlocked
                # whole-extra overwrite — no select_for_update/re-read —
                # racing the ship worker's pr_urls write).
                ticket.merge_extra(set_keys={"pr_title_override": title})
            ticket.ship()
            ticket.save()
    except (TransitionNotAllowed, InvalidTransitionError) as exc:
        return _gate_failure(ticket, exc)
    return None


def _enqueue_ship(ticket: Ticket, title: str) -> ShipEnqueued | ShippingGateFailure:
    """Async ship: enqueue ``execute_ship`` and warn it needs a worker.

    The push + PR are NOT performed here — they run in ``execute_ship``,
    which only fires when a worker drains the django-tasks queue. In a
    no-worker context (e.g. an interactive ``uv run`` invocation) the ship
    silently never completes; the explicit ``warning`` makes that visible
    instead of looking like a successful ship (#708). Use ``--sync`` to
    push + open the PR inline in this process.
    """
    failure = _do_ship_transition(ticket, title)
    if failure is not None:
        return failure
    return ShipEnqueued(
        ticket_id=int(ticket.pk),
        state=str(ticket.state),
        queued=True,
        warning=(
            "Ship was QUEUED, not performed. The branch push and PR creation "
            "run in the `execute_ship` task and will NOT complete until a "
            "worker drains the queue (`t3 <overlay> tasks work-next`). "
            "Re-run with `--sync` to push and open the PR inline now."
        ),
    )


def _ship_exception_detail(executor: ShipExecutor, opened_pr_url: str, failed_detail: str, exc: Exception) -> str:
    if failed_detail:
        return f"{failed_detail}; recording it failed: {exc}"
    if opened_pr_url:
        return f"PR {opened_pr_url} opened but the FSM advance failed: {exc}"
    if executor.opened_pr_url:
        return f"PR {executor.opened_pr_url} opened but ship failed: {exc}"
    return str(exc)


def _ship_sync(ticket: Ticket, title: str) -> ShipExecuted | ShippingGateFailure:
    """Synchronous ship (#708): preflight, push + open the PR, then advance the FSM in one short commit.

    A failed push or PR-open leaves the FSM untouched (#838, #860); a PR whose advance is then
    refused stays recorded, so a re-run adopts it instead of opening a second one.
    """
    opened_pr_url = ""
    failed_detail = ""
    executor = ShipExecutor(ticket, title_override=title)
    try:
        ticket.refresh_from_db()
        try:
            ticket.refuse_unless_shippable()
        except (TransitionNotAllowed, InvalidTransitionError) as exc:
            return _gate_failure(ticket, exc)
        result = executor.run()
        if not result.ok:
            failed_detail = result.detail
            executor.record_opened_pr()
            return _sync_result(ticket, ok=False, detail=result.detail)
        opened_pr_url = result.detail
        try:
            with transaction.atomic():
                fresh = Ticket.objects.select_for_update().get(pk=ticket.pk)
                if fresh.state != Ticket.State.REVIEW_REQUESTED:
                    fresh.complete_ship_after_pr_open()
                    fresh.save()
        except (TransitionNotAllowed, InvalidTransitionError) as exc:
            detail = f"PR {result.detail} opened but the FSM advance was refused: {exc}"
            return _sync_result(ticket, ok=False, detail=detail)
    except Exception as exc:
        logger.exception("Synchronous ship failed")
        if executor.opened_pr_url:
            try:
                executor.record_opened_pr()
            except Exception as record_exc:
                logger.exception("Could not record opened PR")
                detail = (
                    f"PR {executor.opened_pr_url} opened but recording it failed: {record_exc}; "
                    f"original failure: {failed_detail or exc}"
                )
                return _sync_result(ticket, ok=False, detail=detail)
        return _sync_result(
            ticket, ok=False, detail=_ship_exception_detail(executor, opened_pr_url, failed_detail, exc)
        )
    return _sync_result(ticket, ok=True, detail=result.detail)
