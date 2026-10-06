"""The regression corpus's own fresh DB and its schema pre-flight (souliane/teatree#2190).

The corpus's ORM-backed checks (``Worktree``/``Ticket``/``MergeClear`` create)
assert gate behaviour, not the host's state, so :func:`fresh_corpus_db` points
them at an empty temp DB rather than the ambient one, whose migration state the
diff does not control.

The pre-flight applies pending migrations in-process via the existing
sanctioned :func:`teatree.core.gates.schema_guard.migrate_self_db`
(idempotent, non-destructive, runs against the exact connection the checks
read), so a migration-adding PR never breaks the lane again. A migrate failure
fails the corpus LOUD (fail-closed) — never a silent pass against a
half-migrated DB. Split out of ``regression_corpus`` to keep that module under
the module-health LOC cap.
"""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from django.db import connections

from teatree.db.boundary import ControlDbBoundary
from teatree.eval.regression_corpus_models import CheckResult, RegressionCheck

SCHEMA_PREFLIGHT = RegressionCheck(
    failure_class="pinned-regressions lane survives a runtime-schema migration (#2190)",
    origin="https://github.com/souliane/teatree/issues/2190",
    invariant=(
        "the corpus migrates the runtime self-DB current before its ORM checks, "
        "so a migration-adding PR never reds the pre-push lane with OperationalError"
    ),
    predicate=lambda: True,  # never invoked — the pre-flight runs migrate_self_db directly.
    needs_db=True,
)


@contextmanager
def fresh_corpus_db() -> Iterator[None]:
    """Point every file-backed DB alias at one fresh temp file the pre-flight migrates, then restore.

    An in-memory DB is already private to this process, so it is left alone.
    """
    with tempfile.TemporaryDirectory(prefix="t3-pinned-regressions-") as scratch:
        original: list[tuple[str, str]] = []
        for alias in connections:
            connection = connections[alias]
            name = str(connection.settings_dict["NAME"])
            if not ControlDbBoundary(name).file_backed:
                continue
            original.append((alias, name))
            connection.close()
            # One file for every alias, so the config alias reads the schema the pre-flight migrated.
            connection.settings_dict["NAME"] = str(Path(scratch) / "corpus.sqlite3")
        try:
            yield
        finally:
            for alias, name in original:
                connections[alias].close()
                connections[alias].settings_dict["NAME"] = name


def migrate_self_db() -> list[str]:
    """Apply pending migrations to the runtime-resolved self-DB (patchable seam).

    Thin binding of :func:`teatree.core.gates.schema_guard.migrate_self_db` so
    the corpus's pre-flight has one name to call (and tests one name to patch).
    """
    from teatree.core.gates.schema_guard import migrate_self_db as _migrate  # noqa: PLC0415 — deferred: per eval run

    return _migrate()


def schema_preflight_result() -> CheckResult:
    """Migrate the runtime self-DB current; a migrate failure fails the corpus loud.

    Returns a GREEN :class:`CheckResult` when the schema is current (or was
    migrated to current), and a RED one carrying the failure when the in-process
    migrate raises — fail-closed, never a silent pass against a half-migrated DB.
    """
    from teatree.core.gates.schema_guard import SelfDbMigrationError  # noqa: PLC0415 — deferred: loaded per eval run

    try:
        migrate_self_db()
    except SelfDbMigrationError as exc:
        return CheckResult(check=SCHEMA_PREFLIGHT, ok=False, skipped=False, detail=str(exc))
    return CheckResult(check=SCHEMA_PREFLIGHT, ok=True, skipped=False, detail="")


def skipped_preflight_result(reason: str) -> CheckResult:
    """Report the pre-flight as not-run without touching the database (#4005).

    Omitting the row entirely is what made a failed migration pass *silently*: with no
    row, neither a renderer nor a caller could tell the pre-flight had been meant to run
    at all. Naming it as skipped costs nothing — building the row opens no connection, so
    the topology guard still runs strictly before anything reaches the DB.
    """
    return CheckResult(check=SCHEMA_PREFLIGHT, ok=True, skipped=True, detail=reason)


__all__ = [
    "SCHEMA_PREFLIGHT",
    "fresh_corpus_db",
    "migrate_self_db",
    "schema_preflight_result",
    "skipped_preflight_result",
]
