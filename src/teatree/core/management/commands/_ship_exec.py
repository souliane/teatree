"""Ship execution: FSM transition + inline/async push (extracted from ``pr.py``).

The ``pr create`` command validates the deterministic gates, then either
*enqueues* ``execute_ship`` (default) or runs it *inline* (``--sync``).
That ship-execution concern — the ``ship()`` FSM transition and its
atomicity contract with ``execute_ship`` — is its own self-documenting
home, kept here so ``pr.py`` stays within the module-health LOC bar.

Synchronous ship atomicity (#838, #860): ``_ship_sync`` runs the
``ship()`` transition **and** the inline ``execute_ship`` inside a
*single* ``transaction.atomic()`` block. Both a ``ShipExecutor.run()``
exception (#838) and a non-raising structured failure (#860,
``RunnerResult(ok=False)``) roll the ``ship()`` advance back, so the
ship is all-or-nothing and the real cause is structured-surfaced. See
BLUEPRINT § "Synchronous ship atomicity".
"""

from typing import TYPE_CHECKING, TypedDict

from django.db import transaction
from django_fsm import TransitionNotAllowed

from teatree.core.models import InvalidTransitionError, Ticket

if TYPE_CHECKING:
    from teatree.core.tasks import TransitionResult


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


class _ShipExecutionError(RuntimeError):
    """Carries a structured ``execute_ship`` failure out of the atomic block.

    #860: raised inside ``_ship_sync``'s ``transaction.atomic()`` when
    ``execute_ship`` returns ``ok=False`` (a non-raising precondition
    failure) so the shared transaction rolls the ``ship()`` advance back
    rather than committing a partial ``SHIPPED``. ``str(exc)`` is the
    real ``detail``, surfaced by the existing handler.
    """


def _abort_on_ship_failure(result: "TransitionResult") -> None:
    """Raise ``_ShipExecutionError`` when ``execute_ship`` reported failure.

    #860: a non-raising ``RunnerResult(ok=False)`` is a ship failure too;
    raising here aborts ``_ship_sync``'s shared transaction so the
    ``ship()`` advance rolls back instead of committing a partial
    ``SHIPPED`` (no push, no PR).
    """
    if not result.get("ok"):
        raise _ShipExecutionError(str(result.get("detail", "")))


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
        ticket.refresh_from_db()
        error = str(exc) or f"Cannot ship from state '{ticket.state}': FSM not in REVIEWED."
        return ShippingGateFailure(
            allowed=False,
            error=error,
            missing=[],
            hint="Drop --skip-validation so the gate can reconcile the FSM, or record the missing phases.",
        )
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
            "worker drains the queue (`t3 <overlay> tasks work-next-sdk`). "
            "Re-run with `--sync` to push and open the PR inline now."
        ),
    )


def _ship_sync(ticket: Ticket, title: str) -> ShipExecuted | ShippingGateFailure:
    """Synchronous ship: run ``execute_ship`` inline in this process (#708).

    Atomicity (#838): the FSM ``ship()`` transition and the inline
    ``execute_ship`` run inside a **single** ``transaction.atomic()``
    block. Pre-#838 ``_do_ship_transition`` committed ``SHIPPED`` in its
    own transaction and ``execute_ship.call()`` ran afterwards in a
    separate one; a ``ShipExecutor.run()`` exception (a ``git push``
    precondition failure surfaces as ``CommandFailedError``) then left
    ``Ticket.state == SHIPPED`` with no push and no PR — a partial state
    the operator could not safely re-run. Sharing one transaction makes
    the exception roll the FSM advance back, so the ship is all-or-nothing:
    either pushed + PR opened + FSM advanced, or the FSM is untouched.

    Error surfacing (#838): a ``ShipExecutor.run()`` exception is caught
    here and returned as a structured ``ShipExecuted`` (``ok=False`` with
    the real error in ``detail``). Pre-#838 the exception propagated
    unhandled, crashing the ``manage.py`` subprocess so the CLI wrapper
    only surfaced a bare ``rc=1`` with the real cause lost.

    Structured-failure atomicity (#860): ``ShipExecutor.run()`` also has
    *non-raising* precondition exits (``"no code host configured"``,
    ``"no worktree on ticket"``, ``"branch ... already merged into
    base"``) that return ``RunnerResult(ok=False)``. ``execute_ship``
    then returns a normal ``{"ok": False}`` dict — no exception — so
    pre-#860 both atomic blocks committed, leaving the same partial
    ``SHIPPED`` (no push, no PR) #838 closed for the raised path. A
    failing ``execute_ship`` result is a ship failure too, so it must
    roll the ``ship()`` advance back: it is re-raised inside the atomic
    block as ``_ShipExecutionError`` carrying the real ``detail``, so
    both failure paths share one rollback + surfacing path.

    The ``on_commit`` enqueue scheduled by ``ship()`` only fires when the
    block commits (success); ``execute_ship`` is idempotent (re-checks
    state under ``select_for_update``) so a worker later picking it up is
    a safe no-op (state is no longer SHIPPED).
    """
    from teatree.core.tasks import execute_ship  # noqa: PLC0415

    try:
        with transaction.atomic():
            failure = _do_ship_transition(ticket, title)
            if failure is not None:
                return failure
            result = execute_ship.call(int(ticket.pk))
            # #860: a structured ship failure must roll the ``ship()``
            # advance back too — abort so the shared transaction does
            # not commit a partial SHIPPED.
            _abort_on_ship_failure(result)
    except Exception as exc:  # noqa: BLE001
        # Atomicity: the surrounding ``transaction.atomic()`` already
        # rolled the ``ship()`` advance back, so the FSM is NOT left in a
        # partial SHIPPED state. Surface the real cause instead of letting
        # it crash the subprocess into an opaque wrapper-only ``rc=1``.
        ticket.refresh_from_db()
        return ShipExecuted(
            ticket_id=int(ticket.pk),
            state=str(ticket.state),
            synced=True,
            ok=False,
            detail=str(exc),
        )
    ticket.refresh_from_db()
    return ShipExecuted(
        ticket_id=int(ticket.pk),
        state=str(ticket.state),
        synced=True,
        ok=bool(result.get("ok", False)),
        detail=str(result.get("detail", "")),
    )
