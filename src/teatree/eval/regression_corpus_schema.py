"""Runtime self-DB schema pre-flight for the regression corpus (souliane/teatree#2190).

The corpus's ORM-backed checks (``Worktree``/``Ticket``/``MergeClear`` create)
run against the runtime-resolved self-DB. A worktree's auto-isolated DB is
seeded from a snapshot of the canonical DB at its (possibly stale) schema and
migrations are NEVER applied to it — so a PR adding a migration (e.g. the
``last_used_at`` column) left that DB missing the new column/table and every
ORM check raised ``OperationalError: no such column``, redding the
``eval-pinned-regressions`` pre-push lane.

The pre-flight applies pending migrations in-process via the existing
sanctioned :func:`teatree.core.gates.schema_guard.migrate_self_db`
(idempotent, non-destructive, runs against the exact connection the checks
read), so a migration-adding PR never breaks the lane again. A migrate failure
fails the corpus LOUD (fail-closed) — never a silent pass against a
half-migrated DB. Split out of ``regression_corpus`` to keep that module under
the module-health LOC cap.
"""

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


__all__ = ["SCHEMA_PREFLIGHT", "migrate_self_db", "schema_preflight_result"]
