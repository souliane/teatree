"""Is the live DB carrying the schema THIS code needs? — the deploy-order gate (#3901).

``loop/scanners/self_update`` fast-forwards the running install's HEAD on a
cadence (#1249); applying the schema is a *separate* step the ``init`` container
runs at boot. Nothing sequenced the two, so between a merge landing and the next
container restart a live worker could execute code whose models the control DB
did not have — a structural window, not an incidental one.

This module is the predicate that closes it. It is deliberately three-valued:

* ``CURRENT`` — the migration graph is fully applied; work may be admitted.
* ``BEHIND``  — named unapplied migrations; the code is ahead of the DB.
* ``UNKNOWN`` — the probe itself failed.

``UNKNOWN`` exists so a failed probe can never be laundered into "the schema is
fine". A two-valued predicate would have to pick one, and the safe pick collapses
into the ambiguous-empty trap: a caller asking "may I claim?" would read a probe
failure as a green light precisely when it cannot tell. Both non-``CURRENT``
states therefore refuse admission (fail closed), and the never-lockout escape is
the ``schema_readiness_gate_enabled`` kill switch rather than a softer verdict.

The verdict is memoised per alias for :data:`READINESS_TTL_SECONDS` because the
claim chokepoint reads it: walking the migration graph on every claim would trade
one structural bug for a hot-path regression. The window is bounded both ways —
the hot-pull seam calls :func:`invalidate_schema_readiness` the moment the code on
disk moves, which is the only event that can turn a ``CURRENT`` verdict stale.

This module never migrates and never notifies; it only answers. Applying the
migrations is :mod:`teatree.loop.scanners.self_update_schema` (the post-pull
reconcile) or ``t3 <overlay> db migrate``.
"""

import enum
import logging
from dataclasses import dataclass
from time import monotonic

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor

from teatree.config import get_effective_settings

logger = logging.getLogger(__name__)

#: How long a verdict is reused before the migration graph is walked again.
READINESS_TTL_SECONDS = 60.0

#: How many migration labels the refusal text names before it summarises the rest.
_MAX_NAMED_MIGRATIONS = 6

_MEMO: dict[str, tuple[float, "SchemaReadiness"]] = {}


class SchemaState(enum.StrEnum):
    """Whether the live DB carries the schema the running code expects."""

    CURRENT = "current"
    BEHIND = "behind"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SchemaReadiness:
    """One reading of the live DB's schema against the running code's migration graph."""

    state: SchemaState
    pending: tuple[str, ...] = ()
    detail: str = ""

    @property
    def admits_work(self) -> bool:
        return self.state is SchemaState.CURRENT

    def block_reason(self) -> str:
        """The operator-facing refusal, or ``""`` when work may be admitted."""
        if self.state is SchemaState.CURRENT:
            return ""
        if self.state is SchemaState.BEHIND:
            return (
                f"the control DB is {len(self.pending)} migration(s) BEHIND this code "
                f"({_summarise(self.pending)}) — the running process would execute against a "
                f"schema it does not have. Apply them: `t3 <overlay> db migrate`."
            )
        return (
            f"the control DB schema could not be verified against this code ({self.detail}) — "
            f"refusing rather than assuming it is current. Resolve it, or disable the gate with "
            f"`t3 <overlay> config_setting set schema_readiness_gate_enabled false`."
        )


def _summarise(labels: tuple[str, ...]) -> str:
    named = ", ".join(labels[:_MAX_NAMED_MIGRATIONS])
    overflow = len(labels) - _MAX_NAMED_MIGRATIONS
    return f"{named} and {overflow} more" if overflow > 0 else named


def pending_migrations(alias: str = DEFAULT_DB_ALIAS) -> list[str]:
    """Return ``"<app>.<name>"`` for every unapplied migration on *alias*.

    Empty list means the schema is current. Uses Django's own
    :class:`MigrationExecutor` so the result matches ``showmigrations``.
    """
    connection = connections[alias]
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    plan = executor.migration_plan(targets)
    return [f"{migration.app_label}.{migration.name}" for migration, _backwards in plan]


def read_schema_readiness(alias: str = DEFAULT_DB_ALIAS) -> SchemaReadiness:
    """Walk the migration graph now and classify the result — never memoised.

    Deliberately asymmetric with ``gates.schema_guard.doctor_check_self_db_migrations``,
    which treats an ``OperationalError`` as WARN-and-pass: a DB that is absent/offline
    is a valid session-start state for a *report*, but not a licence to admit work. So
    during a DB blip the doctor reads green while claims park. Both directions are the
    safe one for their own caller, and the disagreement is bounded by
    :data:`READINESS_TTL_SECONDS` — the probe re-runs the moment the blip clears.
    """
    try:
        pending = tuple(pending_migrations(alias))
    except Exception as exc:  # noqa: BLE001 — a failed probe is UNKNOWN, never a silent CURRENT
        return SchemaReadiness(state=SchemaState.UNKNOWN, detail=f"{exc.__class__.__name__}: {exc}")
    if pending:
        return SchemaReadiness(state=SchemaState.BEHIND, pending=pending)
    return SchemaReadiness(state=SchemaState.CURRENT)


def cached_schema_readiness(alias: str = DEFAULT_DB_ALIAS) -> SchemaReadiness:
    """The memoised verdict for *alias*, re-probing once its TTL has elapsed."""
    now = monotonic()
    entry = _MEMO.get(alias)
    if entry is not None and now < entry[0]:
        return entry[1]
    readiness = read_schema_readiness(alias)
    _MEMO[alias] = (now + READINESS_TTL_SECONDS, readiness)
    return readiness


def invalidate_schema_readiness(alias: str | None = None) -> None:
    """Drop the memoised verdict — call this the moment the code on disk moves."""
    if alias is None:
        _MEMO.clear()
    else:
        _MEMO.pop(alias, None)


def schema_readiness_gate_enabled() -> bool:
    """The kill switch, failing CLOSED when the config store cannot be read."""
    try:
        return bool(get_effective_settings().schema_readiness_gate_enabled)
    except Exception:  # noqa: BLE001 — an unreadable switch must not silently open the gate
        logger.warning("schema-readiness kill switch unreadable — keeping the gate enabled")
        return True


def schema_admission_block_reason(alias: str = DEFAULT_DB_ALIAS) -> str:
    """Why work must not be admitted against *alias* right now, or ``""`` to admit.

    The claim chokepoint's face of :func:`cached_schema_readiness`. Reads the kill
    switch OUTSIDE the memo so flipping it takes effect on the next claim rather
    than after the TTL.
    """
    if not schema_readiness_gate_enabled():
        return ""
    return cached_schema_readiness(alias).block_reason()


__all__ = [
    "READINESS_TTL_SECONDS",
    "SchemaReadiness",
    "SchemaState",
    "cached_schema_readiness",
    "invalidate_schema_readiness",
    "pending_migrations",
    "read_schema_readiness",
    "schema_admission_block_reason",
    "schema_readiness_gate_enabled",
]
