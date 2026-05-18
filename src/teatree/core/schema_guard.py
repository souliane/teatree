"""Self-DB schema pre-flight for the sanctioned merge path (#869).

The sanctioned merge commands ``t3 teatree ticket clear`` and
``t3 teatree ticket merge`` write/read ``MergeClear``/``MergeAudit`` rows.
When the teatree self-DB (the dogfood control DB) has unapplied migrations
those tables do not exist, and the ORM call surfaces a raw
``django.db.utils.OperationalError: no such table: teatree_merge_clear``
traceback.

Since #863 mechanically prohibits raw ``gh pr merge`` on teatree-managed
tickets, that opaque failure blocks the *entire* merge pipeline with no
actionable signal. This module provides a cheap pre-flight that converts
the silent traceback into a clear, sanctioned-remediation error and a
``t3 doctor`` check that surfaces the gap proactively at session start.
"""

import typer
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor

_REMEDIATION = (
    "Run the sanctioned non-destructive migrate:\n"
    "  uv --directory <teatree-clone> run python manage.py migrate --no-input\n"
    "(or `t3 <overlay> resetdb` only if losing all local ticket/session state "
    "is acceptable). `t3 doctor check` flags this gap at session start."
)


class SelfDbMigrationError(RuntimeError):
    """Raised when the teatree self-DB has unapplied migrations.

    Carries the unapplied migration labels so the caller can render an
    actionable message instead of a raw ``OperationalError`` traceback.
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


def require_current_schema(alias: str = DEFAULT_DB_ALIAS) -> None:
    """Fail closed with an actionable message if the self-DB is unmigrated.

    Called as a pre-flight by the sanctioned merge commands so an
    unmigrated self-DB can never produce a raw ``no such table`` traceback
    on the critical merge path.
    """
    pending = pending_migrations(alias)
    if not pending:
        return
    listed = ", ".join(pending)
    msg = (
        f"teatree self-DB has {len(pending)} unapplied migration(s): {listed}. "
        f"The sanctioned merge path (ticket clear/merge) needs a current schema "
        f"(e.g. the MergeClear/MergeAudit tables) and refuses to run against a "
        f"stale DB rather than fail with an opaque traceback.\n{_REMEDIATION}"
    )
    raise SelfDbMigrationError(msg)


def doctor_check_self_db_migrations(alias: str = DEFAULT_DB_ALIAS) -> bool:
    """``t3 doctor`` surface for the self-DB schema pre-flight (#869).

    Returns ``True`` (check passed) when the schema is current. Returns
    ``False`` with a ``FAIL`` line naming the pending migrations so the
    gap is caught at session start instead of mid-merge. A DB that is
    absent/offline is a valid state — it ``WARN``s and does not fail.
    """
    try:
        pending = pending_migrations(alias)
    except Exception as exc:  # noqa: BLE001 — DB absent/offline is a valid state, not a doctor crash.
        typer.echo(f"WARN  Could not inspect self-DB migrations: {exc.__class__.__name__}: {exc}")
        return True
    if not pending:
        return True
    typer.echo(
        f"FAIL  teatree self-DB has {len(pending)} unapplied migration(s): {', '.join(pending)}. "
        f"The sanctioned merge path needs a current schema — run "
        f"`uv --directory <teatree-clone> run python manage.py migrate --no-input`."
    )
    return False
