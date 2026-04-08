"""Generic Django database provisioning engine.

Implements the reference-DB + template-copy pattern with a 4-strategy
fallback chain: DSLR snapshot → local dump → remote dump → CI dump.

Overlays configure the engine via ``DjangoDbImportConfig``; the engine
does the rest.  No Django imports — shells out to ``manage.py``.
"""

import enum
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from teatree.utils import bad_artifacts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DjangoDbImportConfig:
    ref_db_name: str
    ticket_db_name: str
    main_repo_path: str
    dump_dir: str
    dump_glob: str
    ci_dump_glob: str
    snapshot_tool: str = "dslr"
    remote_db_url: str = ""
    migrate_env_extra: dict[str, str] = field(default_factory=dict)
    dump_timeout: int = 1800
    dslr_snapshot: str = ""  # Force a specific DSLR snapshot name (skip auto-discovery)
    dump_path: str = ""  # Force a specific .pgsql dump file (skip DSLR and dump discovery)


@dataclass(frozen=True)
class _RestoreContext:
    cfg: DjangoDbImportConfig
    dslr_cmd: list[str]
    dslr_env: dict[str, str]
    pg_host: str
    pg_user: str
    pg_env: dict[str, str]


# ---------------------------------------------------------------------------
# Low-level Postgres helpers
# ---------------------------------------------------------------------------


def _pg_args() -> tuple[str, str, dict[str, str]]:
    from teatree.utils.db import pg_env, pg_host, pg_user  # noqa: PLC0415

    return pg_host(), pg_user(), pg_env()


def _local_db_url(db_name: str) -> str:
    from urllib.parse import quote  # noqa: PLC0415

    from teatree.utils.db import pg_host, pg_user  # noqa: PLC0415

    pw = os.environ.get("POSTGRES_PASSWORD", "")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgres://{pg_user()}:{quote(pw, safe='')}@{pg_host()}:{port}/{db_name}"


def _ensure_ref_db(ref_db: str, pg_host: str, pg_user: str, pg_env: dict[str, str]) -> None:
    subprocess.run(
        ["createdb", "-h", pg_host, "-U", pg_user, ref_db],
        env=pg_env,
        capture_output=True,
        check=False,
    )


def _terminate_connections(db_name: str, pg_host: str, pg_user: str, pg_env: dict[str, str]) -> None:
    subprocess.run(
        [
            "psql",
            "-h",
            pg_host,
            "-U",
            pg_user,
            "-d",
            "postgres",
            "-v",
            f"dbname={db_name}",
            "-c",
            (
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :'dbname' AND pid <> pg_backend_pid()"
            ),
        ],
        env=pg_env,
        capture_output=True,
        check=False,
    )


def _copy_ref_to_ticket(ctx: _RestoreContext) -> bool:
    cfg = ctx.cfg
    subprocess.run(
        ["dropdb", "-h", ctx.pg_host, "-U", ctx.pg_user, "--if-exists", cfg.ticket_db_name],
        env=ctx.pg_env,
        capture_output=True,
        check=False,
    )
    _terminate_connections(cfg.ref_db_name, ctx.pg_host, ctx.pg_user, ctx.pg_env)
    result = subprocess.run(
        ["createdb", "-h", ctx.pg_host, "-U", ctx.pg_user, cfg.ticket_db_name, "-T", cfg.ref_db_name],
        env=ctx.pg_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  WARNING: Template copy failed: {result.stderr.strip()}", file=sys.stderr)  # noqa: T201
        return False
    return True


# ---------------------------------------------------------------------------
# DSLR helpers
# ---------------------------------------------------------------------------


def _find_dslr_cmd(tool_name: str, main_repo_path: str = "") -> list[str]:
    """Return a command prefix for invoking dslr.

    Prefers ``uv run`` (resolves project venv where dslr + psycopg live).
    When *main_repo_path* is provided, passes ``--directory`` so uv
    resolves from the project that actually has dslr as a dependency,
    not from the current working directory.
    Bare ``dslr`` on PATH is unreliable (pyenv shims, missing deps).
    Honour ``DSLR_CMD`` env var as an explicit override.
    """
    dslr = os.environ.get("DSLR_CMD", "")
    if dslr and shutil.which(dslr):
        return [dslr]
    if shutil.which("uv"):
        cmd = ["uv"]
        if main_repo_path:
            cmd.extend(["--directory", main_repo_path])
        cmd.extend(["run", tool_name])
        return cmd
    return []


def _dslr_env(ref_db: str) -> dict[str, str]:
    url = _local_db_url(ref_db)
    return {**os.environ, "DATABASE_URL": url, "DSLR_DB_URL": url}


def _dslr_snap_name(ref_db: str) -> str:
    today = datetime.now(tz=UTC).strftime("%Y%m%d")
    return f"{today}_{ref_db}"


def _dslr_artifact_key(snap_name: str) -> str:
    return f"dslr:{snap_name}"


def _find_dslr_snapshots(dslr_cmd: list[str], env: dict[str, str], ref_db: str) -> list[str]:
    """Return matching DSLR snapshots sorted newest-first, excluding bad artifacts."""
    suffix = f"_{ref_db}"
    result = subprocess.run([*dslr_cmd, "list"], env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if token.endswith(suffix) and not bad_artifacts.is_bad(_dslr_artifact_key(token)):
            names.append(token)
    names.sort(reverse=True)
    return names


def _is_env_error(stderr: str) -> bool:
    """Return True if the error is environmental (connection, auth), not data corruption."""
    env_patterns = [
        "connection refused",
        "could not connect",
        "password authentication failed",
        "SSL",
        "ssl",
        "no pg_hba.conf entry",
        "timeout expired",
        "server closed the connection",
    ]
    lower = stderr.lower()
    return any(p.lower() in lower for p in env_patterns)


def _restore_ref_from_dslr(dslr_cmd: list[str], env: dict[str, str], snap_name: str) -> tuple[bool, bool, str]:
    """Restore a DSLR snapshot. Returns (success, is_env_error, stderr)."""
    result = subprocess.run(
        [*dslr_cmd, "restore", snap_name],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True, False, ""
    return False, _is_env_error(result.stderr), result.stderr.strip()


def _take_dslr_snapshot(dslr_cmd: list[str], env: dict[str, str], ref_db: str) -> None:
    snap_name = _dslr_snap_name(ref_db)
    print(f"  Taking DSLR snapshot: {snap_name}")  # noqa: T201
    subprocess.run(
        [*dslr_cmd, "snapshot", "-y", snap_name],
        env=env,
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Dump validation
# ---------------------------------------------------------------------------


def validate_dump(dump_path: Path) -> bool:
    if dump_path.stat().st_size == 0:
        return False
    result = subprocess.run(
        ["pg_restore", "-l", str(dump_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if "could not read" in (result.stderr or ""):
        print(f"  WARNING: Dump appears truncated: {dump_path.name} (delete and re-fetch)")  # noqa: T201
        return False
    return True


# ---------------------------------------------------------------------------
# Migration with selective faking
# ---------------------------------------------------------------------------

_MAX_MIGRATE_RETRIES = 20


class _MigrateResult(enum.Enum):
    APPLIED = "applied"
    ALREADY_MIGRATED = "already_migrated"
    FAILED = "failed"


def _extract_failing_migration(stdout: str) -> str | None:
    match = re.search(r"Applying (\w+\.\w+)\.\.\.", stdout)
    return match.group(1) if match else None


def _migrate_reference_db(main_repo: str, ref_db: str, extra_env: dict[str, str]) -> _MigrateResult:
    """Migrate the reference database.

    Returns APPLIED when migrations ran, ALREADY_MIGRATED when the DB was
    already up to date (callers skip the post-migrate DSLR snapshot), or
    FAILED when migration cannot proceed.
    """
    manage_py = Path(main_repo) / "manage.py"
    if not manage_py.is_file():
        print(f"  Skipping reference DB migration (no manage.py in {main_repo})")  # noqa: T201
        return _MigrateResult.ALREADY_MIGRATED

    ref_db_url = _local_db_url(ref_db)
    run_env = {**os.environ, "DATABASE_URL": ref_db_url, "DISABLE_DATABASE_SSL": "True", **extra_env}

    print(f"  Migrating reference DB ({ref_db}) using main repo...")  # noqa: T201
    for _attempt in range(_MAX_MIGRATE_RETRIES):
        result = subprocess.run(
            ["python", "manage.py", "migrate", "--no-input"],
            cwd=main_repo,
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            if "No migrations to apply" in result.stdout:
                print("  Reference DB already up to date (no migrations applied).")  # noqa: T201
                return _MigrateResult.ALREADY_MIGRATED
            print("  Reference DB migrated.")  # noqa: T201
            return _MigrateResult.APPLIED

        combined = f"{result.stdout}\n{result.stderr}"
        failure_reason = _try_fake_failing_migration(combined, result.stdout, main_repo, run_env)
        if failure_reason:
            print(f"  WARNING: {failure_reason}")  # noqa: T201
            return _MigrateResult.FAILED

    print("  WARNING: Reference DB migration exhausted retries, skipping.")  # noqa: T201
    return _MigrateResult.FAILED


def _try_fake_failing_migration(combined: str, stdout: str, main_repo: str, run_env: dict[str, str]) -> str:
    """Try to fake a failing migration. Returns empty string on success, error message on failure."""
    config_markers = ("ModuleNotFoundError", "ImproperlyConfigured", "DJANGO_SETTINGS_MODULE", "No module named")
    if any(m in combined for m in config_markers):
        return "Cannot migrate reference DB (config error), skipping."

    if "already exists" not in combined and "does not exist" not in combined:
        return "Cannot migrate reference DB (non-fakeable error), skipping."

    failing = _extract_failing_migration(stdout)
    if not failing:
        return "Cannot identify failing migration, skipping reference migration."

    app_label, migration_name = failing.split(".", 1)
    reason = "schema already exists" if "already exists" in combined else "table absent from dump"
    print(f"  Faking {failing} on reference DB ({reason})...")  # noqa: T201
    subprocess.run(
        ["python", "manage.py", "migrate", app_label, migration_name, "--fake"],
        cwd=main_repo,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    return ""


# ---------------------------------------------------------------------------
# Restore-and-copy pipeline
# ---------------------------------------------------------------------------


def _restore_ref_and_copy(ctx: _RestoreContext, dump_path: str, label: str) -> bool:
    from teatree.utils.db import db_restore  # noqa: PLC0415

    cfg = ctx.cfg
    try:
        db_restore(cfg.ref_db_name, dump_path)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        bad_artifacts.mark_bad(dump_path)
        print(f"  BAD ARTIFACT: {label} marked bad (delete: rm {dump_path})")  # noqa: T201
        print(f"    Restore error: {exc}", file=sys.stderr)  # noqa: T201
        return False
    migrate_result = _migrate_reference_db(cfg.main_repo_path, cfg.ref_db_name, cfg.migrate_env_extra)
    if migrate_result is _MigrateResult.FAILED:
        bad_artifacts.mark_bad(dump_path)
        print(f"  BAD ARTIFACT: {label} marked bad (delete: rm {dump_path})")  # noqa: T201
        return False
    if ctx.dslr_cmd and migrate_result is _MigrateResult.APPLIED:
        _take_dslr_snapshot(ctx.dslr_cmd, ctx.dslr_env, cfg.ref_db_name)
    if _copy_ref_to_ticket(ctx):
        print(f"  Created {cfg.ticket_db_name} from {label}.")  # noqa: T201
        return True
    return False


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------

# Prevent retrying remote dump in the same process after a failure.
_remote_dump_failed: bool = False


def _try_restore_from_dump_path(ctx: _RestoreContext) -> bool:
    """Restore from an explicit dump file path (skip all auto-discovery)."""
    from teatree.utils.db import db_restore  # noqa: PLC0415

    dump = Path(ctx.cfg.dump_path)
    if not dump.is_file():
        print(f"  ERROR: Dump file not found: {dump}")  # noqa: T201
        return False
    print(f"  Restoring from explicit dump: {dump}")  # noqa: T201
    _ensure_ref_db(ctx.cfg.ref_db_name, ctx.pg_host, ctx.pg_user, ctx.pg_env)
    if not db_restore(ctx.cfg.ref_db_name, str(dump)):
        return False
    migrate_result = _migrate_reference_db(ctx.cfg.main_repo_path, ctx.cfg.ref_db_name, ctx.cfg.migrate_env_extra)
    if migrate_result is _MigrateResult.FAILED:
        return False
    if ctx.dslr_cmd and migrate_result is _MigrateResult.APPLIED:
        _take_dslr_snapshot(ctx.dslr_cmd, ctx.dslr_env, ctx.cfg.ref_db_name)
    return _copy_ref_to_ticket(ctx)


def _resolve_dslr_snapshots(ctx: _RestoreContext) -> list[str]:
    if ctx.cfg.dslr_snapshot:
        return [ctx.cfg.dslr_snapshot]
    return _find_dslr_snapshots(ctx.dslr_cmd, ctx.dslr_env, ctx.cfg.ref_db_name)


def _log_dslr_restore_failure(snap_name: str, *, is_env: bool, stderr: str) -> None:
    if is_env:
        print(f"  WARNING: DSLR restore failed (environment error, not marking bad): {snap_name}")  # noqa: T201
    else:
        bad_artifacts.mark_bad(_dslr_artifact_key(snap_name))
        print(f"  BAD ARTIFACT: DSLR snapshot '{snap_name}' marked bad (delete: dslr delete {snap_name})")  # noqa: T201
    if stderr:
        logger.warning("DSLR restore stderr for %s: %s", snap_name, stderr)
        print(f"    Restore error: {stderr[:200]}")  # noqa: T201


def _try_restore_from_dslr(ctx: _RestoreContext, *, skip_dslr: bool) -> bool:
    if skip_dslr:
        logger.info("DSLR restore skipped (skip_dslr=True)")
        return False
    if not ctx.dslr_cmd:
        logger.info("DSLR restore skipped (no snapshot tool configured)")
        return False
    _ensure_ref_db(ctx.cfg.ref_db_name, ctx.pg_host, ctx.pg_user, ctx.pg_env)
    snapshots = _resolve_dslr_snapshots(ctx)
    if not snapshots:
        return False
    for snap_name in snapshots:
        print(f"  Restoring {ctx.cfg.ref_db_name} from DSLR snapshot: {snap_name}")  # noqa: T201
        ok, is_env, stderr = _restore_ref_from_dslr(ctx.dslr_cmd, ctx.dslr_env, snap_name)
        if not ok:
            _log_dslr_restore_failure(snap_name, is_env=is_env, stderr=stderr)
            continue
        migrate_result = _migrate_reference_db(ctx.cfg.main_repo_path, ctx.cfg.ref_db_name, ctx.cfg.migrate_env_extra)
        if migrate_result is _MigrateResult.FAILED:
            print(f"  WARNING: Migration failed after DSLR restore of {snap_name} (not marking snapshot bad)")  # noqa: T201
            continue
        if migrate_result is _MigrateResult.APPLIED:
            _take_dslr_snapshot(ctx.dslr_cmd, ctx.dslr_env, ctx.cfg.ref_db_name)
        else:
            print("  Skipping DSLR snapshot (DB already migrated, snapshot is up to date).")  # noqa: T201
        if _copy_ref_to_ticket(ctx):
            print(f"  Created {ctx.cfg.ticket_db_name} from DSLR snapshot.")  # noqa: T201
            return True
        print(f"  WARNING: Template copy after DSLR {snap_name} failed, trying older...")  # noqa: T201
    logger.warning("All DSLR snapshots failed for %s", ctx.cfg.ref_db_name)
    print("  WARNING: All DSLR snapshots failed. Trying local dump fallback...")  # noqa: T201
    return False


def _try_restore_from_local_dump(ctx: _RestoreContext) -> bool:
    dump_dir = Path(ctx.cfg.dump_dir)
    if not dump_dir.is_dir():
        logger.info("Local dump dir %s does not exist", dump_dir)
        return False
    dumps = sorted(
        (p for p in dump_dir.glob(ctx.cfg.dump_glob) if validate_dump(p) and not bad_artifacts.is_bad(str(p))),
        key=lambda p: p.name,
        reverse=True,
    )
    if not dumps:
        for zd in dump_dir.glob(ctx.cfg.dump_glob):
            if zd.stat().st_size == 0:
                print(f"  WARNING: Skipping 0-byte dump: {zd.name} (delete it)")  # noqa: T201
        return False
    for dump in dumps:
        print(f"  Restoring from local dump: {dump.name}")  # noqa: T201
        if _restore_ref_and_copy(ctx, str(dump), f"local dump ({dump.name})"):
            return True
        print(f"  WARNING: Local dump {dump.name} failed, trying older...")  # noqa: T201
    logger.warning("All local dumps failed for %s", ctx.cfg.ref_db_name)
    print("  WARNING: All local dumps failed. Trying remote dump...")  # noqa: T201
    return False


def _try_fetch_remote_dump(ctx: _RestoreContext) -> bool:
    """Fetch a fresh dump from the remote DB into dump_dir.

    Returns True if a new dump file was saved (caller should re-run
    local dump strategy). Returns False on failure.
    """
    global _remote_dump_failed  # noqa: PLW0603
    cfg = ctx.cfg
    if not cfg.remote_db_url:
        logger.info("Remote dump skipped (no remote_db_url configured)")
        return False
    if _remote_dump_failed:
        print("  Skipping remote dump (already failed in this run).")  # noqa: T201
        return False
    dump_dir = Path(cfg.dump_dir)
    try:
        today = datetime.now(tz=UTC).strftime("%Y%m%d")
        dump_path = dump_dir / f"{today}_{cfg.ref_db_name}.pgsql"
        print(f"  Dumping from remote DB ({cfg.ref_db_name})...")  # noqa: T201
        dump_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["pg_dump", "-Fc", "-f", str(dump_path), cfg.remote_db_url],
            capture_output=True,
            timeout=cfg.dump_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _remote_dump_failed = True
        print(f"  WARNING: pg_dump timed out after {cfg.dump_timeout}s.")  # noqa: T201
        return False
    if result.returncode == 0:
        size_mb = dump_path.stat().st_size / 1_000_000 if dump_path.exists() else 0
        print(f"  Saved {dump_path.name} ({size_mb:.0f}MB)")  # noqa: T201
        return True
    _remote_dump_failed = True
    raw = result.stderr or b""
    stderr_text = raw.decode(errors="replace").strip() if isinstance(raw, bytes) else raw.strip()
    logger.warning("pg_dump failed (rc=%d) for %s: %s", result.returncode, cfg.ref_db_name, stderr_text)
    print(f"  WARNING: pg_dump failed: {stderr_text}")  # noqa: T201
    return False


def _try_restore_from_ci_dump(ctx: _RestoreContext) -> bool:
    ci_dumps = sorted(
        Path(ctx.cfg.main_repo_path).glob(ctx.cfg.ci_dump_glob),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not ci_dumps:
        return False
    ci_dump = ci_dumps[0]
    print(f"  Restoring from CI dump (last resort): {ci_dump.name}")  # noqa: T201
    if _restore_ref_and_copy(ctx, str(ci_dump), f"CI dump ({ci_dump.name})"):
        return True
    logger.warning("CI dump restore failed for %s", ctx.cfg.ref_db_name)
    print("  WARNING: CI dump restore failed.")  # noqa: T201
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reset_remote_dump_state() -> None:
    """Reset the remote dump failure flag (for testing)."""
    global _remote_dump_failed  # noqa: PLW0603
    _remote_dump_failed = False


def _parse_dslr_snapshots(stdout: str) -> dict[str, list[str]]:
    """Parse ``dslr list`` output, group snapshot names by tenant (suffix after date)."""
    by_tenant: dict[str, list[str]] = {}
    for line in stdout.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if not token:
            continue
        # Snapshot names: <date>_<tenant>  e.g. "20260401_development-acme"
        if "_" in token:
            tenant = token.split("_", maxsplit=1)[1]
            by_tenant.setdefault(tenant, []).append(token)
    for names in by_tenant.values():
        names.sort(reverse=True)
    return by_tenant


def prune_dslr_snapshots(*, keep: int = 1, snapshot_tool: str = "dslr", main_repo_path: str = "") -> list[str]:
    """Delete old DSLR snapshots, keeping the *keep* newest per tenant.

    Returns a list of deleted snapshot names.
    """
    dslr_cmd = _find_dslr_cmd(snapshot_tool, main_repo_path)
    if not dslr_cmd:
        return []
    result = subprocess.run([*dslr_cmd, "list"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []
    by_tenant = _parse_dslr_snapshots(result.stdout)
    deleted: list[str] = []
    for tenant, names in by_tenant.items():
        for old in names[keep:]:
            print(f"  Pruning DSLR snapshot: {old} (tenant={tenant})")  # noqa: T201
            subprocess.run([*dslr_cmd, "delete", "-y", old], capture_output=True, check=False)
            deleted.append(old)
    return deleted


def _warn_slow_path(label: str) -> None:
    """Print a prominent warning when a non-DSLR (slow) restore path executes."""
    print(f"  WARNING [SLOW PATH]: {label}", file=sys.stderr)  # noqa: T201
    print("  DSLR snapshots are the expected fast path. This operation is significantly slower.", file=sys.stderr)  # noqa: T201


def django_db_import(
    cfg: DjangoDbImportConfig,
    *,
    skip_dslr: bool = False,
    slow_import: bool = False,
    allow_remote_dump: bool = False,
) -> bool:
    """Import a Django database with fallback chain.

    By default only DSLR snapshots are tried — the fast, expected path.
    Non-DSLR fallbacks (pg_restore from dump, remote dump download) are
    gated behind *slow_import=True* to prevent accidentally triggering
    multi-minute operations.  Pass ``--slow-import`` on the CLI to enable.

    Remote dumps additionally require *allow_remote_dump=True*.

    Returns True on success, False if no source was available.
    """
    dslr_cmd = _find_dslr_cmd(cfg.snapshot_tool, cfg.main_repo_path) if cfg.snapshot_tool else []
    pg_host, pg_user, pg_env = _pg_args()
    dslr_e = _dslr_env(cfg.ref_db_name) if dslr_cmd else {}
    ctx = _RestoreContext(
        cfg=cfg,
        dslr_cmd=dslr_cmd,
        dslr_env=dslr_e,
        pg_host=pg_host,
        pg_user=pg_user,
        pg_env=pg_env,
    )

    if (cfg.dump_path and _try_restore_from_dump_path(ctx)) or (
        not cfg.dump_path and _try_restore_from_dslr(ctx, skip_dslr=skip_dslr)
    ):
        return True

    # --- Non-DSLR fallbacks (slow) — gated behind --slow-import --------
    if not slow_import:
        print(  # noqa: T201
            "\n  DSLR restore failed or unavailable. Non-DSLR fallbacks are disabled by default.\n"
            "  Non-DSLR paths (pg_restore, remote dump) take minutes instead of seconds.\n"
            "  To allow slow fallback paths, re-run with: --slow-import\n",
            file=sys.stderr,
        )
        return False

    _warn_slow_path("Falling back to pg_restore from local dump file.")
    if _try_restore_from_local_dump(ctx):
        return True

    if allow_remote_dump:
        _warn_slow_path("Downloading fresh dump from remote database (pg_dump over network).")
        if _try_fetch_remote_dump(ctx) and _try_restore_from_local_dump(ctx):
            return True

    _warn_slow_path("Trying CI dump as last resort (pg_restore).")
    if _try_restore_from_ci_dump(ctx):
        return True

    dump_dir = Path(cfg.dump_dir)
    print(f"  ERROR: No database source available for '{cfg.ref_db_name}'.")  # noqa: T201
    print(f"  - No local DSLR snapshot for {cfg.ref_db_name}")  # noqa: T201
    print(f"  - No dump in {dump_dir}/")  # noqa: T201
    print(f"  - No CI dump matching {cfg.ci_dump_glob} in {cfg.main_repo_path}")  # noqa: T201
    print()  # noqa: T201
    if cfg.remote_db_url:
        print("  To fetch a fresh dump from the remote DB, re-run with --slow-import.")  # noqa: T201
    else:
        print("  Configure remote_db_url in DjangoDbImportConfig to enable remote dump fetching.")  # noqa: T201
    return False
