"""Sequence the schema behind a hot pull, or shout (#3901).

:mod:`teatree.loop.scanners.self_update` fast-forwards the running install's HEAD
on a cadence (#1249); applying the schema is a *separate* step the ``init``
container runs at boot. Nothing ordered the two, so a merge that lands a migration
left a live worker running code whose models the control DB did not carry — the
window PR #3900's three migrations went through.

This is the missing sequencer, run the moment a clone actually advances. The
control DB is a local SQLite state DB, so applying its pending migrations in place
is the same non-destructive forward-only ``migrate_self_db`` the merge path's
pre-flight already trusts (#2006) — not a new risk. What is new is that the
alternative is no longer silence: an unverifiable schema or a migrate that fails
pages the owner AND leaves the claim gate refusing, so the worker stays parked
instead of dispatching agents that will crash on the first missing relation.

The reconcile always invalidates the readiness memo, since the code on disk moving
is the one event that can turn a cached ``CURRENT`` verdict into a lie.
"""

import enum
import logging
from dataclasses import dataclass

from teatree.core.gates.schema_guard import SelfDbMigrationError, migrate_self_db
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.schema_readiness import (
    SchemaState,
    invalidate_schema_readiness,
    read_schema_readiness,
    schema_admission_block_reason,
)

logger = logging.getLogger(__name__)


class SchemaReconcileState(enum.StrEnum):
    """What the post-pull reconcile did about the freshly-pulled code's schema."""

    CURRENT = "current"
    MIGRATED = "migrated"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SchemaReconcile:
    """One clone's post-pull schema reconcile."""

    state: SchemaReconcileState
    applied: tuple[str, ...] = ()
    detail: str = ""

    @property
    def blocks_work(self) -> bool:
        return self.state is SchemaReconcileState.FAILED


def reconcile_schema_after_pull(*, label: str, head_sha: str) -> SchemaReconcile:
    """Bring the control DB up to the freshly-pulled code, or page the owner."""
    invalidate_schema_readiness()
    readiness = read_schema_readiness()
    if readiness.state is SchemaState.CURRENT:
        return SchemaReconcile(state=SchemaReconcileState.CURRENT)
    if readiness.state is SchemaState.UNKNOWN:
        return _fail(label=label, head_sha=head_sha, pending=(), detail=readiness.detail)
    try:
        applied = tuple(migrate_self_db())
    except SelfDbMigrationError as exc:
        return _fail(label=label, head_sha=head_sha, pending=readiness.pending, detail=str(exc))
    invalidate_schema_readiness()
    logger.info("self_update %s applied %d pending migration(s) after pull", label, len(applied))
    return SchemaReconcile(state=SchemaReconcileState.MIGRATED, applied=applied)


def retry_pending_reconcile(*, label: str, head_sha: str) -> SchemaReconcile | None:
    """Re-attempt a reconcile an earlier tick left unresolved, or ``None`` if there is none.

    Only a clone that ADVANCES reconciles, and the pull is never replayed — HEAD has
    already moved. So a reconcile that failed once (a transient locked SQLite is
    enough) would never be retried: every later tick reads ``up_to_date`` and the
    claim gate stays shut until a human intervenes. This is that retry, run on the
    ticks where nothing advanced.

    It is gated on :func:`schema_admission_block_reason` — the claim chokepoint's OWN
    face of the verdict — and not on the raw readiness read, because that function is
    what consults the ``schema_readiness_gate_enabled`` kill switch. Gating on the bare
    verdict would leave the never-lockout escape half-effective: on the box the switch
    exists for, one whose probe MISFIRES, the operator would stand the gate down and
    still get a migrate attempt and an ``action_needed`` row every tick from the same
    bad verdict. Switch off means this mechanism is off too.

    The gate reuses the memoised verdict, so it never walks the migration graph more
    than once per :data:`READINESS_TTL_SECONDS` — but the TTL is on the order of the
    tick, so on a quiet box expect roughly one graph walk per minute, not a free dict
    lookup. Passing the clone's recorded HEAD keeps the owner page keyed exactly as the
    first failure was, so a persistent park pages once rather than once per tick.
    """
    if not schema_admission_block_reason():
        return None
    return reconcile_schema_after_pull(label=label, head_sha=head_sha)


def _fail(*, label: str, head_sha: str, pending: tuple[str, ...], detail: str) -> SchemaReconcile:
    logger.error("self_update %s left the control DB behind its code: %s", label, detail)
    _page_owner(label=label, head_sha=head_sha, pending=pending, detail=detail)
    return SchemaReconcile(state=SchemaReconcileState.FAILED, detail=detail)


def schema_behind_code_message(*, label: str, pending: tuple[str, ...], detail: str) -> str:
    """The owner-facing page for a clone whose code outran the control DB."""
    listed = ", ".join(pending) if pending else "(the pending set could not be read)"
    return (
        f"`{label}` pulled new code but its control DB could NOT be brought to that code's "
        f"schema: {detail}. Pending: {listed}. The claim path is refusing new work until this "
        f"clears, so the factory is PARKED, not broken — apply the migrations "
        f"(`t3 <overlay> db migrate`) or restart the stack so `init` applies them."
    )


def _page_owner(*, label: str, head_sha: str, pending: tuple[str, ...], detail: str) -> None:
    """Escalate to the owner; a dead transport must never break the tick it rides on."""
    try:
        notify_user(
            schema_behind_code_message(label=label, pending=pending, detail=detail),
            kind=NotifyKind.INFO,
            idempotency_key=f"schema_behind_code:{label}:{head_sha[:12]}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.exception("could not page the owner about %s's schema drift", label)


__all__ = [
    "SchemaReconcile",
    "SchemaReconcileState",
    "reconcile_schema_after_pull",
    "retry_pending_reconcile",
    "schema_behind_code_message",
]
