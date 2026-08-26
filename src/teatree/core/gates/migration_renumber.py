"""Is this pending migration the applied one, renumbered? — souliane/teatree#4591.

``django-linear-migrations`` enforces one leaf, so a branch whose migration sits
behind ``main``'s leaf MUST renumber on rebase. Django records applied migrations
by NAME, so the rename strands every database that already applied the old
number: the schema change is present, the new name reads as pending, ``migrate``
re-runs it and fails on the object it just found. CI never sees it — a fresh DB
applies the renumbered migration cleanly — so it bites only the dev boxes that
ever ran the branch, and it arrives as a bare ``duplicate column`` with nothing
pointing at the rename.

Detection is read-only and evidenced: a pair is claimed only when the applied row
carries the SAME suffix under a DIFFERENT number AND no migration file carries
that old name any more. Remediation is deliberately NOT automatic — a blind
``--fake`` would mask a genuinely unapplied migration whose column happens to
exist for an unrelated reason — so it lives behind
``t3 teatree db reconcile-renumbered --apply``.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from teatree.core.schema_readiness import pending_migrations

#: Failure text that means "the object this migration creates is already there" —
#: the only class of migrate failure a renumber can explain.
COLLISION_MARKERS: tuple[str, ...] = (
    "duplicate column",
    "already exists",
    "duplicate key",
    "duplicate index",
)

_NUMBERED = re.compile(r"(\d+)_(.+)")


@dataclass(frozen=True, slots=True)
class RenumberedMigration:
    """One pending migration and the stale applied row that is the same migration."""

    app: str
    applied_name: str
    pending_name: str
    applied_at: str

    @property
    def pending_label(self) -> str:
        return f"{self.app}.{self.pending_name}"

    @property
    def applied_label(self) -> str:
        return f"{self.app}.{self.applied_name}"


def looks_like_a_collision(failure_text: str) -> bool:
    """Whether *failure_text* is the "object already there" class a renumber explains."""
    lowered = failure_text.lower()
    return any(marker in lowered for marker in COLLISION_MARKERS)


def _suffix(name: str) -> str:
    match = _NUMBERED.fullmatch(name)
    return match.group(2) if match else ""


def _number(name: str) -> str:
    match = _NUMBERED.fullmatch(name)
    return match.group(1) if match else ""


def match_renumbered(
    *,
    pending: Sequence[str],
    applied: Iterable[tuple[str, str, str]],
    on_disk: frozenset[tuple[str, str]],
) -> list[RenumberedMigration]:
    """Pair each pending migration with the stale applied row that IS that migration.

    *pending* holds ``"<app>.<name>"`` labels, *applied* the ``django_migrations``
    rows as ``(app, name, applied_at)``, and *on_disk* every ``(app, name)`` the
    migration loader can see. The old name being absent from *on_disk* is what
    makes this evidence rather than a guess about two similarly-named migrations.
    """
    rows = sorted(applied)
    pairs: list[RenumberedMigration] = []
    for label in pending:
        app, _, name = label.partition(".")
        suffix = _suffix(name)
        if not suffix:
            continue
        pairs.extend(
            RenumberedMigration(app=app, applied_name=row_name, pending_name=name, applied_at=applied_at)
            for row_app, row_name, applied_at in rows
            if row_app == app
            and _suffix(row_name) == suffix
            and _number(row_name) != _number(name)
            and (row_app, row_name) not in on_disk
        )
    return pairs


def renumber_hint(pairs: Sequence[RenumberedMigration]) -> str:
    """The operator-facing diagnosis, or ``""`` when nothing was detected."""
    if not pairs:
        return ""
    lines = [
        f"{pair.pending_label} appears to be {pair.applied_label} RENUMBERED: this DB recorded "
        f"the old name on {pair.applied_at or 'an unrecorded date'}, no migration file carries it "
        f"any more, and the new name reads as pending."
        for pair in pairs
    ]
    lines.append(
        "Reconcile without touching the schema: `t3 teatree db reconcile-renumbered --apply` "
        "(equivalently, one `migrate --fake <app> <name>` per pair)."
    )
    return "\n".join(lines)


def renumbered_migrations(alias: str = DEFAULT_DB_ALIAS) -> list[RenumberedMigration]:
    """Read *alias* and report every pending migration that is a renumbered applied one."""
    connection = connections[alias]
    executor = MigrationExecutor(connection)
    squashes = {
        key for key, migration in executor.loader.disk_migrations.items() if getattr(migration, "replaces", None)
    }
    pending = [label for label in pending_migrations(alias) if tuple(label.split(".", 1)) not in squashes]
    rows = MigrationRecorder(connection).migration_qs.values_list("app", "name", "applied")
    applied = [(app, name, str(applied_at or "")) for app, name, applied_at in rows]
    return match_renumbered(pending=pending, applied=applied, on_disk=frozenset(executor.loader.disk_migrations))


def renumber_hint_for(alias: str, failure_text: str) -> str:
    """The hint to append to a failed migrate, or ``""`` — never masks the failure.

    Best-effort by construction: this runs on a path that is ALREADY reporting an
    error, so a detector that raised would replace a real diagnosis with its own.
    """
    if not looks_like_a_collision(failure_text):
        return ""
    try:
        return renumber_hint(renumbered_migrations(alias))
    except Exception:  # noqa: BLE001 — a hint that raised would bury the failure it annotates
        return ""


__all__ = [
    "COLLISION_MARKERS",
    "RenumberedMigration",
    "looks_like_a_collision",
    "match_renumbered",
    "renumber_hint",
    "renumber_hint_for",
    "renumbered_migrations",
]
