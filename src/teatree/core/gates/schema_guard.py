"""Self-DB schema pre-flight for the sanctioned merge path (#869, #2006).

The sanctioned merge commands ``t3 teatree ticket clear`` and
``t3 teatree ticket merge`` write/read ``MergeClear``/``MergeAudit`` rows.
When the teatree self-DB (the dogfood control DB) has unapplied migrations
those tables do not exist, and the ORM call surfaces a raw
``django.db.utils.OperationalError: no such table: teatree_merge_clear``
traceback.

Since #863 mechanically prohibits raw ``gh pr merge`` on teatree-managed
tickets, that opaque failure blocks the *entire* merge pipeline with no
actionable signal. This module provides a cheap pre-flight that converts
the silent traceback into a clear outcome and a ``t3 doctor`` check that
surfaces the gap proactively at session start.

The recurring footgun (#2006): the live editable install tracks the main
clone, so a keystone merge of a migration-adding PR advances the install
over a new migration and leaves the self-DB behind. Every subsequent
sanctioned operation then crashed with ``no such table`` until a human ran
``t3 teatree db migrate``. :func:`require_current_schema` now self-heals
that state — it applies the pending self-DB migrations in place (the same
non-destructive, forward-only ``migrate_self_db`` the manual command runs)
before proceeding. Auto-migrating the self-DB is safe: it is a local SQLite
state DB, not a tenant/product DB, and the migrate is fail-closed — a
genuine migrate failure raises :class:`SelfDbMigrationError` with the
actionable remediation rather than proceeding against a half-migrated DB.
``migrate_self_db`` (exposed as ``t3 teatree db migrate``) stays the
explicit, always-available manual self-rescue.
"""

import typer
from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import OperationalError

_REMEDIATION = (
    "Run the sanctioned non-destructive migrate against the runtime self-DB:\n"
    "  t3 teatree db migrate\n"
    "(or `t3 <overlay> resetdb` only if losing all local ticket/session state "
    "is acceptable). `t3 doctor check` flags this gap at session start."
)


class SelfDbMigrationError(RuntimeError):
    """Raised when the teatree self-DB has unapplied migrations or a migrate fails.

    On the read path it carries the unapplied migration labels so the caller
    can render an actionable message instead of a raw ``OperationalError``
    traceback. On the write path (:func:`migrate_self_db`) it wraps the
    underlying migrate failure so the consumer fails *closed* rather than
    proceeding against a half-migrated DB.
    """


def pending_migrations(alias: str = DEFAULT_DB_ALIAS) -> list[str]:
    """Return ``"<app>.<name>"`` for every unapplied migration on *alias*.

    Empty list ⇒ the schema is current. Uses Django's own
    :class:`MigrationExecutor` so the result matches ``showmigrations``.
    """
    connection = connections[alias]
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    plan = executor.migration_plan(targets)
    return [f"{migration.app_label}.{migration.name}" for migration, _backwards in plan]


def migrate_self_db(alias: str = DEFAULT_DB_ALIAS) -> list[str]:
    """Apply pending migrations to the runtime-resolved control DB in-process.

    The always-available, non-destructive self-rescue for a stale runtime
    self-DB. It runs ``migrate --no-input`` *in the running process* against
    the connection named by *alias* — the exact connection
    :func:`pending_migrations` reads — so "migrate then re-check" converges on
    the same DB. This is the structural fix for the lockout: every prior
    migrate wrapper either targeted a different DB than the runtime resolves
    (``uv --directory <clone>`` auto-isolates a worktree-anchored editable
    install onto a sibling DB) or was destructive (``resetdb``).

    Returns the ``"<app>.<name>"`` labels that were pending (and are now
    applied); an empty list when the DB was already current (idempotent
    no-op). Existing rows are preserved — ``migrate`` never drops live data.

    Fail-closed: a genuine migrate failure is re-raised as
    :class:`SelfDbMigrationError` (never swallowed), so a consumer that
    migrates-then-proceeds cannot proceed against a half-migrated DB.
    """
    pending = pending_migrations(alias)
    if not pending:
        return []
    try:
        call_command("migrate", "--no-input", database=alias, verbosity=0)
    except Exception as exc:
        msg = (
            f"teatree self-DB migrate failed on alias {alias!r} "
            f"({len(pending)} pending): {exc.__class__.__name__}: {exc}. "
            f"The DB is left UNMIGRATED; the sanctioned merge path stays fail-closed (#870)."
        )
        raise SelfDbMigrationError(msg) from exc
    return pending


def require_current_schema(alias: str = DEFAULT_DB_ALIAS) -> None:
    """Ensure the self-DB schema is current, auto-applying pending migrations (#2006).

    Called as a pre-flight by the sanctioned merge commands so an
    unmigrated self-DB can never produce a raw ``no such table`` traceback
    on the critical merge path. When the live editable install has advanced
    over a new migration, the self-DB falls behind; rather than refuse and
    force a manual ``t3 teatree db migrate``, this applies the pending
    migrations in place via :func:`migrate_self_db` (probe-gated,
    idempotent, non-destructive forward-only) and proceeds.

    Fail LOUD if the migrate itself fails: :func:`migrate_self_db` raises
    :class:`SelfDbMigrationError` on a genuine migrate failure, which is
    re-raised with the actionable remediation so the caller surfaces it
    instead of proceeding against a half-migrated DB.
    """
    pending = pending_migrations(alias)
    if not pending:
        return
    try:
        migrate_self_db(alias)
    except SelfDbMigrationError as exc:
        listed = ", ".join(pending)
        msg = (
            f"teatree self-DB has {len(pending)} unapplied migration(s): {listed}. "
            f"The sanctioned merge path (ticket clear/merge) needs a current schema "
            f"(e.g. the MergeClear/MergeAudit tables); auto-applying them failed: {exc}\n"
            f"{_REMEDIATION}"
        )
        raise SelfDbMigrationError(msg) from exc


def doctor_check_self_db_migrations(alias: str = DEFAULT_DB_ALIAS) -> bool:
    """``t3 doctor`` surface for the self-DB schema pre-flight (#869).

    Returns ``True`` (check passed) when the schema is current. Returns
    ``False`` with a ``FAIL`` line naming the pending migrations so the
    gap is caught at session start instead of mid-merge. A DB that is
    absent/offline (``OperationalError``) is a valid state — it ``WARN``s
    and passes. Any other error (a wrong alias, an ORM regression) is a
    real defect, so the check fails *closed* (``FAIL``/``False``) rather
    than report the schema current by swallowing the error (#1987).
    """
    try:
        pending = pending_migrations(alias)
    except OperationalError as exc:
        # DB absent/offline is a valid session-start state — WARN, do not fail.
        typer.echo(f"WARN  Could not inspect self-DB migrations: {exc.__class__.__name__}: {exc}")
        return True
    except Exception as exc:  # noqa: BLE001 — any non-connection error is a real defect; fail closed.
        typer.echo(
            f"FAIL  Self-DB migration check errored: {exc.__class__.__name__}: {exc}. "
            f"This is a misconfiguration (wrong DB alias) or an ORM regression, "
            f"not a benign DB-absent state — resolve it before relying on the merge path."
        )
        return False
    if not pending:
        return True
    typer.echo(
        f"FAIL  teatree self-DB has {len(pending)} unapplied migration(s): {', '.join(pending)}. "
        f"The sanctioned merge path needs a current schema — run `t3 teatree db migrate`."
    )
    return False
