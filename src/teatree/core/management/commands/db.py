"""Database operations: migrate, refresh, restore from CI, reset passwords, introspect."""

import json
import os
import sys

import typer
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django_typer.management import TyperCommand, command

from teatree.core.gates.db_approval_gate import ApprovalScope, require_approval
from teatree.core.gates.schema_guard import SelfDbMigrationError, migrate_self_db
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.types import SqlRow
from teatree.utils.approval import ApprovalRefusedError

#: Leading SQL keywords allowed past the cheap pre-filter of ``db query``.
#: This is a *best-effort* guard, not a proof of read-only-ness: leading-token
#: matching cannot see a data-modifying CTE (``WITH t AS (DELETE … RETURNING …)``)
#: or ``SELECT … INTO newtbl``. ``with`` and ``values`` are deliberately
#: excluded because a ``WITH``-prefixed statement can carry a writable CTE and
#: ``VALUES`` is never needed for introspection. The binding safety guarantee
#: is the enforced read-only transaction in :meth:`Command.query`; this filter
#: only rejects the obvious cases early with a clearer message (#774).
_READ_ONLY_LEADING = frozenset({"select", "pragma", "explain"})


def _is_read_only(sql: str) -> bool:
    """True iff *sql* passes the cheap leading-keyword read-only pre-filter.

    Keyed on the leading token (case-insensitive) and a single-statement
    shape — a trailing ``;`` is tolerated but an embedded ``;`` (statement
    batching, the classic read-then-write smuggle) is refused so a
    ``SELECT 1; DROP TABLE x`` can never slip through.

    A ``PRAGMA`` *setter* (``PRAGMA name = value``) is refused while a
    read-only introspection PRAGMA (``PRAGMA table_info(t)``) is allowed:
    the read-only transaction does **not** stop ``PRAGMA writable_schema=1``
    (a connection-level switch, not a row write), so the setter form must
    be caught here. This is still best-effort only — a data-modifying CTE
    or ``SELECT … INTO`` starts with an allowed token, so the binding
    guarantee is the enforced read-only transaction in
    :meth:`Command.query`, not this function.
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped or ";" in stripped:
        return False
    leading = stripped.split(None, 1)[0].lower()
    if leading not in _READ_ONLY_LEADING:
        return False
    # A PRAGMA containing '=' is a setter (e.g. ``PRAGMA writable_schema=1``,
    # ``PRAGMA query_only=OFF``) — a write the read-only transaction cannot
    # block. Read-only introspection PRAGMAs never assign a value. Caveat:
    # benign maintenance PRAGMAs without '=' (``PRAGMA optimize``,
    # ``wal_checkpoint``) still pass both layers — harmless for introspection.
    return not (leading == "pragma" and "=" in stripped)


def _read_only_guard_sql(vendor: str) -> tuple[str | None, str | None]:
    """Return ``(enter_sql, exit_sql)`` enforcing read-only for *vendor*.

    postgresql: ``SET TRANSACTION READ ONLY`` — any write (a data-modifying
    CTE, ``SELECT … INTO``) raises ``DatabaseError``. Scoped to the
    transaction, so no explicit exit statement is needed.

    sqlite: ``PRAGMA query_only = ON`` — writes raise ``DatabaseError``;
    reset with ``OFF`` afterwards so the connection is not left crippled
    for later commands sharing it (e.g. the test connection).

    any other vendor: ``(None, None)`` — the enforced read-only transaction
    is unavailable, so the leading-keyword pre-filter in
    :func:`_is_read_only` is the only guard.

    A pure ``vendor -> (sql, sql)`` mapping so every branch is testable
    without a live database of that vendor.
    """
    if vendor == "postgresql":
        return "SET TRANSACTION READ ONLY", None
    if vendor == "sqlite":
        return "PRAGMA query_only = ON", "PRAGMA query_only = OFF"
    return None, None


def _run_read_only(sql: str) -> list[SqlRow]:
    """Execute *sql* inside an enforced read-only transaction; return rows.

    The read-only-ness is enforced by the database (see
    :func:`_read_only_guard_sql`), not by parsing SQL. Wrapped in
    ``transaction.atomic`` so the read-only scope is a real transaction
    boundary and any partial effect is rolled back.
    """
    enter_sql, exit_sql = _read_only_guard_sql(connection.vendor)
    with transaction.atomic():
        with connection.cursor() as cursor:
            if enter_sql is not None:
                cursor.execute(enter_sql)
            try:
                cursor.execute(sql)
                columns = [col[0] for col in cursor.description] if cursor.description else []
                rows = [dict(zip(columns, record, strict=True)) for record in cursor.fetchall()]
            finally:
                if exit_sql is not None:
                    cursor.execute(exit_sql)
        return rows


class Command(TyperCommand):
    @command()
    def migrate(self) -> None:
        """Apply pending migrations to the runtime self-DB, non-destructively.

        The always-available self-rescue for a stale runtime control DB —
        the exact gap that locks out the sanctioned merge path
        (``ticket clear``/``merge`` refuse on ANY pending migration). It
        delegates to :func:`teatree.core.gates.schema_guard.migrate_self_db`, which
        runs ``migrate --no-input`` *in this process* against the same
        connection the merge gate reads, so "migrate then re-check"
        converges on one DB.

        Unlike ``resetdb`` this drops nothing — live ticket/session/lease
        rows survive. Unlike the old ``uv --directory <clone>`` wrapper it
        cannot target a different (auto-isolated) DB than the runtime
        resolves. Dispatched via teatree-core (``python -m teatree``) so it
        reaches the runtime self-DB regardless of which overlay invokes it.

        Fail-closed: a real migrate failure exits non-zero with the captured
        error, never leaving a half-migrated DB look like a success.
        """
        try:
            applied = migrate_self_db()
        except SelfDbMigrationError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(1) from exc
        if not applied:
            self.stdout.write("Self-DB already current — no migrations to apply.")
            return
        self.stdout.write(f"Applied {len(applied)} migration(s): {', '.join(applied)}")

    @command()
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def refresh(  # noqa: PLR0913 — django-typer command: every param is a CLI flag mapped 1:1 to the public `db refresh` surface (path/dslr/dump/force/fresh-dump/user-authorized); the arg list IS the CLI contract, not an internal design smell (same rationale as ticket.py:clear).
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        dslr_snapshot: str = typer.Option("", help="Force a specific DSLR snapshot name."),
        dump_path: str = typer.Option("", help="Path to a .pgsql dump file to restore from."),
        *,
        force: bool = False,
        fresh_dump: bool = typer.Option(
            default=False,
            help="Pull a fresh dump from the remote DEV environment for this tenant. "
            "Requires explicit per-invocation approval on every run.",
        ),
        user_authorized: str = typer.Option(
            "",
            help="Id of the user who recorded an explicit DbApproval for this "
            "exact op+tenant (#953). Lets a non-TTY caller satisfy the #777 "
            "gate via the recorded-approval channel; consumed single-use and "
            "audited. Empty ⇒ interactive-TTY approval is required instead.",
        ),
    ) -> str:
        """Re-import the worktree database from DSLR snapshot or dump.

        Without --force: tries DSLR restore first (fast), then full reimport.
        With --force: drops existing DB first, then reimports from scratch.
        Use --dslr-snapshot to force a specific snapshot (skip auto-discovery).
        Use --dump-path to restore from a specific dump file.
        Use --fresh-dump to pull a fresh dump from the remote DEV env — this
        is the only sanctioned remote-dump path and it requires an explicit
        per-invocation approval (#777). The approval has two sanctioned
        channels of the same gate: a human at a TTY typing ``yes``, or a
        recorded single-use user ``DbApproval`` re-presented via
        --user-authorized <id> (#953). An unattended agent can never
        self-approve either channel.
        """
        worktree = resolve_worktree(path)
        overlay = get_overlay()
        strategy = overlay.get_db_import_strategy(worktree)
        if strategy is None:
            self.stderr.write("No DB import strategy configured in the overlay.")
            raise SystemExit(1)

        if fresh_dump:
            tenant = str(strategy.get("source_database", "")) or "<tenant>"
            prompt = (
                "FRESH REMOTE DUMP REQUESTED.\n"
                f"  Source environment : DEV (remote)\n"
                f"  Tenant / source DB : {tenant}\n"
                f"  Target worktree DB : {worktree.db_name}\n"
                "This pulls gigabytes over the network from the shared DEV database "
                "and overwrites the target DB. It must be explicitly approved every run."
            )
            try:
                require_approval(
                    prompt,
                    ApprovalScope(op="fresh-dump", tenant=tenant, user_authorized=user_authorized),
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                )
            except ApprovalRefusedError as exc:
                self.stderr.write(f"Fresh remote dump aborted: {exc}")
                raise SystemExit(1) from exc

        self.stdout.write(f"Refreshing DB '{worktree.db_name}' (force={force})...")

        # Set overlay env vars so pg tools can connect with the right credentials
        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        os.environ.update(overlay.get_env_extra(worktree))

        # Run the overlay's import logic
        # #955: --fresh-dump must force slow_import. The remote-dump branch
        # in DjangoDbImporter.run() sits *after* the `if not slow_import:
        # return False` guard (which itself follows the early DSLR return),
        # so without this the flag silently degrades to "restore the stale
        # local DSLR snapshot" instead of fetching a fresh remote dump.
        success = overlay.db_import(
            worktree,
            force=force,
            slow_import=fresh_dump,
            dslr_snapshot=dslr_snapshot,
            dump_path=dump_path,
            approve_remote_dump=fresh_dump,
        )
        if not success:
            self.stderr.write(f"DB import failed for {worktree.db_name}. Check output above for details.")
            raise SystemExit(1)

        # Run post-DB steps (migrations, collectstatic, etc.)
        for step in overlay.get_post_db_steps(worktree):
            self.stdout.write(f"  Running post-DB step: {step.name}")
            step.callable()

        # Reset passwords
        reset_step = overlay.get_reset_passwords_command(worktree)
        if reset_step:  # pragma: no branch
            self.stdout.write("  Resetting passwords...")
            reset_step.callable()

        # FSM transition
        worktree.db_refresh()
        worktree.save()
        return f"DB refreshed for {worktree.db_name}"

    @command()
    def approve(
        self,
        op: str = typer.Argument(help="The DB op to authorize (e.g. `fresh-dump`)."),
        tenant: str = typer.Argument(help="The tenant / source database the op is scoped to."),
        *,
        approver: str = typer.Option(
            ...,
            "--approver",
            help=(
                "Id of the human user recording the approval. Refused if it names a "
                "maker/coding-agent/loop role — the executing agent can never "
                "self-authorize the op (#953, mirrors MergeClear §17.8 / approve-on-behalf #960)."
            ),
        ),
    ) -> str:
        """Record a single-use ``DbApproval`` that satisfies the #777 gate without a TTY (#953/#126).

        The recorded-approval channel is the no-TTY satisfier for
        ``db refresh --fresh-dump``: a chat-only operator records the
        approval here, then the agent re-runs ``db refresh --fresh-dump
        --user-authorized <id>`` which consumes the row single-use. The
        scope is normalized identically at record and consume, so the
        recorded ``(op, tenant)`` matches the gate's expected scope (named
        in its refusal message) regardless of case/whitespace.
        """
        from teatree.core.models.db_approval import DbApproval, DbApprovalError  # noqa: PLC0415

        try:
            approval = DbApproval.record(op, tenant, approver)
        except DbApprovalError as err:
            self.stderr.write(f"Refused: {err}")
            raise SystemExit(1) from err
        return f"OK recorded DbApproval id={approval.pk} op={approval.op!r} tenant={approval.tenant!r}"

    @command(name="restore-ci")
    def restore_ci(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Restore the worktree database from the latest CI dump."""
        worktree = resolve_worktree(path)
        overlay = get_overlay()
        strategy = overlay.get_db_import_strategy(worktree)
        if strategy is None:
            self.stderr.write("No DB import strategy configured in the overlay.")
            raise SystemExit(1)

        # Use db_import with a hint to skip DSLR/local and go straight to CI
        success = overlay.db_import(worktree, force=True)
        if not success:
            self.stderr.write(f"CI restore failed for {worktree.db_name}.")
            raise SystemExit(1)
        worktree.db_refresh()
        worktree.save()
        return f"DB restored from CI for {worktree.db_name}"

    @command(name="reset-passwords")
    def reset_passwords(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Reset all user passwords to a known dev value."""
        worktree = resolve_worktree(path)
        overlay = get_overlay()
        step = overlay.get_reset_passwords_command(worktree)
        if not step:
            self.stderr.write("No reset-passwords command configured in the overlay.")
            raise SystemExit(1)
        step.callable()
        return f"Passwords reset for worktree {worktree.repo_path}"

    @command()
    def query(self, sql: str) -> None:
        """Run a read-only SQL query against the control DB; emit rows as JSON.

        The query runs through the live Django connection, so it resolves
        the *same* control DB the shipping gate reads. Canonical vs
        worktree-isolated is decided once, at settings-load time, by
        ``teatree.paths.CANONICAL_DB`` — there is no separate resolver to
        drift from ``pr create`` / ``lifecycle visit-phase``. This removes
        the ``manage.py shell -c "..."`` detour that forced weaker
        API-only introspection during handoffs (#774).

        Two-layer read-only enforcement (defense in depth):

        Layer 1 is a best-effort leading-keyword pre-filter: it rejects the
        obvious write/DDL cases early with a clear message — only a single
        ``SELECT``/``PRAGMA``/``EXPLAIN`` statement gets past it, and a
        ``PRAGMA`` setter (``=``) is rejected here too.

        Layer 2 is the binding guarantee: the statement runs inside an
        enforced read-only transaction (Postgres ``SET TRANSACTION READ
        ONLY``, SQLite ``PRAGMA query_only=ON``). A data-modifying CTE
        (``WITH t AS (DELETE … RETURNING …)``) or ``SELECT … INTO`` that
        slips past layer 1 is still blocked by the database itself —
        enforcement does not depend on parsing SQL.

        A write path needs a separate, explicitly-guarded command, never
        this one.
        """
        if not _is_read_only(sql):
            self.stderr.write(
                "Refused: 'db query' is read-only. Only a single "
                "SELECT/PRAGMA/EXPLAIN statement is allowed. A write/DDL path "
                "needs a separate, explicitly-guarded command.",
            )
            raise SystemExit(1)

        try:
            rows = _run_read_only(sql)
        except DatabaseError as exc:
            # Reached when a write smuggled past the pre-filter (e.g. a
            # data-modifying CTE) is rejected by the read-only transaction.
            self.stderr.write(f"Refused: query blocked by read-only transaction: {exc}")
            raise SystemExit(1) from exc

        self.stdout.write(json.dumps(rows, default=str))

    @command()
    def shell(self) -> None:
        """Drop into a Django shell against the resolved (gate) control DB.

        Delegates to Django's own ``shell`` so the same connection and
        worktree-isolated-vs-canonical DB the gate reads is reused — never
        a separately-resolved sqlite file (the #774 asymmetry that caused
        global ``t3`` and worktree ``manage.py`` to disagree).
        """
        call_command("shell")
