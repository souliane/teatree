"""Generic Django database provisioning engine.

Implements the reference-DB + template-copy pattern with a 4-strategy
fallback chain: DSLR snapshot -> local dump -> remote dump -> CI dump.

Overlays configure the engine via ``DjangoDbImportConfig``; the engine
does the rest.  No Django imports -- shells out to ``manage.py``.

User-facing CLI output flows through ``self.stdout`` / ``self.stderr``
on ``DjangoDbImporter`` (Django ``BaseCommand`` pattern), which keeps
the source free of bare ``print`` calls and lets tests capture output
by passing an ``io.StringIO``.

DSLR snapshot helpers live in the sibling ``dslr`` module.
"""

import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from typing import TextIO

from teatree.utils import bad_artifacts
from teatree.utils.django_db import dslr as _dslr
from teatree.utils.django_db import reconcile as _reconcile
from teatree.utils.django_db.config import DjangoDbImportConfig
from teatree.utils.django_db.helpers import _ensure_ref_db, _local_db_url, _pg_args, _terminate_connections
from teatree.utils.django_db.migrate import _MAX_MIGRATE_RETRIES, _MigrateResult
from teatree.utils.django_db.reconcile import is_config_error as _is_config_error
from teatree.utils.django_db.restore import validate_dump
from teatree.utils.django_db.runner import runner_prefix
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)


class DjangoDbImporter:
    """Provision a Django DB by building a ref DB then template-copying it.

    Mirrors Django's ``BaseCommand`` pattern: instances own ``stdout`` and
    ``stderr`` streams, so all user-facing progress flows through
    ``self.stdout.write(...)`` / ``self.stderr.write(...)`` instead of
    bare ``print()``.  Tests pass ``io.StringIO`` to capture output.

    Strategy chain (newest first): DSLR snapshot → local dump → remote
    dump → CI dump.  Non-DSLR fallbacks are gated behind ``slow_import``
    because each takes minutes instead of seconds.
    """

    def __init__(
        self,
        cfg: DjangoDbImportConfig,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.cfg = cfg
        self.stdout: TextIO = stdout if stdout is not None else sys.stdout
        self.stderr: TextIO = stderr if stderr is not None else sys.stderr
        self.dslr_cmd: list[str] = (
            _dslr.find_dslr_cmd(cfg.snapshot_tool, cfg.main_repo_path) if cfg.snapshot_tool else []
        )
        self.dslr_env: dict[str, str] = _dslr.dslr_env(cfg.ref_db_name) if self.dslr_cmd else {}
        pg_host, pg_user, pg_env = _pg_args()
        self.pg_host = pg_host
        self.pg_user = pg_user
        self.pg_env = pg_env
        self._remote_dump_failed = False
        self._migrate_via_docker = False

    # ------- low-level template copy / migration --------------------------

    def _copy_ref_to_ticket(self) -> bool:
        cfg = self.cfg
        # `dropdb --force` (PG 13+) terminates active connections atomically before
        # dropping — otherwise reconnecting services (backend containers attached
        # to the ticket DB) race with pg_terminate_backend and the DB stays up,
        # causing the subsequent createdb to fail with "database already exists".
        drop_result = run_allowed_to_fail(
            ["dropdb", "-h", self.pg_host, "-U", self.pg_user, "--if-exists", "--force", cfg.ticket_db_name],
            env=self.pg_env,
            expected_codes=None,
        )
        if drop_result.returncode != 0:
            self.stderr.write(f"  WARNING: dropdb failed: {drop_result.stderr.strip()}\n")
            return False
        _terminate_connections(cfg.ref_db_name, self.pg_host, self.pg_user, self.pg_env)
        result = run_allowed_to_fail(
            ["createdb", "-h", self.pg_host, "-U", self.pg_user, cfg.ticket_db_name, "-T", cfg.ref_db_name],
            env=self.pg_env,
            expected_codes=None,
        )
        if result.returncode != 0:
            self.stderr.write(f"  WARNING: Template copy failed: {result.stderr.strip()}\n")
            return False
        return True

    def _take_dslr_snapshot(self) -> None:
        snap_name = _dslr.dslr_snap_name(self.cfg.ref_db_name)
        self.stdout.write(f"  Taking DSLR snapshot: {snap_name}\n")
        run_allowed_to_fail([*self.dslr_cmd, "snapshot", "-y", snap_name], env=self.dslr_env, expected_codes=None)

    def _migrate_reference_db(self) -> _MigrateResult:
        """Migrate the reference database.

        Returns APPLIED when migrations ran, ALREADY_MIGRATED when the DB was
        already up to date (callers skip the post-migrate DSLR snapshot), or
        FAILED when migration cannot proceed.
        """
        cfg = self.cfg
        manage_py = Path(cfg.main_repo_path) / "manage.py"
        if not manage_py.is_file():
            self.stdout.write(f"  Skipping reference DB migration (no manage.py in {cfg.main_repo_path})\n")
            return _MigrateResult.ALREADY_MIGRATED

        ref_db_url = _local_db_url(cfg.ref_db_name)
        # souliane/teatree#959: strip DJANGO_SETTINGS_MODULE from the inherited
        # environment. The reference-DB migrate runs `manage.py` from the MAIN
        # CLONE, but `db refresh` is typically invoked from a provisioned
        # worktree whose env-cache exports a worktree-specific settings module
        # (e.g. `<proj>.settings_local`). That module exists only inside the
        # worktree, so inheriting it crashes the migrate subprocess with
        # `ModuleNotFoundError`, the restore pipeline aborts, and the ticket
        # DB is never cloned. The main clone's `manage.py` already applies its
        # own default settings module via `os.environ.setdefault(...)`; let it
        # win. An overlay that genuinely needs a non-default module passes it
        # explicitly through ``cfg.migrate_env_extra`` (merged last, so it
        # always wins over the strip).
        inherited_env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        run_env = {**inherited_env, "DATABASE_URL": ref_db_url, "DISABLE_DATABASE_SSL": "True", **cfg.migrate_env_extra}

        self.stdout.write(f"  Migrating reference DB ({cfg.ref_db_name}) using main repo...\n")
        for _attempt in range(_MAX_MIGRATE_RETRIES):
            result = self._run_migrate(["manage.py", "migrate", "--no-input"], run_env)
            if result.returncode == 0:
                if "No migrations to apply" in result.stdout:
                    self.stdout.write("  Reference DB already up to date (no migrations applied).\n")
                    return _MigrateResult.ALREADY_MIGRATED
                self.stdout.write("  Reference DB migrated.\n")
                return _MigrateResult.APPLIED

            combined = f"{result.stdout}\n{result.stderr}"
            # souliane/teatree#1977: a host config/import error means the
            # selected interpreter's venv is unverified and dep-incomplete (a
            # stale uv-built in-project ``.venv`` django may leak through but
            # celery does not). Don't trust it — retry the whole migrate inside
            # the repo-canonical docker image, where every dependency is baked
            # in, then re-classify. This switch happens once; the --fake step
            # then runs in docker too.
            if _is_config_error(combined) and not self._migrate_via_docker and cfg.dockerized_migrate is not None:
                self.stdout.write("  Host migrate failed to import deps; retrying inside the docker image...\n")
                self._migrate_via_docker = True
                continue

            # souliane/teatree#1038: a master renumber (a migration inserted
            # earlier, bumping later numbers) makes the snapshot's old-numbered
            # applied record fail Django's `check_consistent_history` BEFORE any
            # forward migrate runs — "X is applied before its dependency Y". When
            # the conflict is a provable pure renumber, reconcile the stale
            # record and retry the migrate; otherwise fall through to the normal
            # fake/divergence handling (the reconcile NEVER masks real drift).
            if _reconcile.reconcile_renumbered_migration(
                combined, run_env, run_managepy=self._run_migrate, stdout=self.stdout
            ):
                continue

            failure_reason = self._try_fake_failing_migration(combined, result.stdout, run_env)
            if failure_reason:
                # souliane/teatree#959: surface the real subprocess output on
                # FAILED so the operator can see the actual error
                # (ModuleNotFoundError, schema mismatch, etc.) — the generic
                # one-liner alone was unactionable.
                self.stdout.write(f"  WARNING: {failure_reason}\n")
                if result.stdout:
                    self.stdout.write(f"    migrate stdout:\n{result.stdout}\n")
                if result.stderr:
                    self.stderr.write(f"    migrate stderr:\n{result.stderr}\n")
                return _MigrateResult.FAILED

        self.stdout.write("  WARNING: Reference DB migration exhausted retries, skipping.\n")
        return _MigrateResult.FAILED

    def _run_migrate(self, manage_args: list[str], run_env: dict[str, str]) -> CompletedProcess[str]:
        """Run one ``manage.py`` invocation via the selected runner.

        Dispatches to the overlay's dockerized runner once core has switched to
        it (#1977); otherwise runs on the host with the dependency-manager-aware
        prefix (#1973).
        """
        if self._migrate_via_docker and self.cfg.dockerized_migrate is not None:
            return self.cfg.dockerized_migrate(manage_args, run_env)
        runner = runner_prefix(Path(self.cfg.main_repo_path))
        return run_allowed_to_fail(
            [*runner, *manage_args], cwd=self.cfg.main_repo_path, env=run_env, expected_codes=None
        )

    def _try_fake_failing_migration(self, combined: str, stdout: str, run_env: dict[str, str]) -> str:
        """Try to fake a failing migration. Returns empty string on success, error message on failure."""
        if _is_config_error(combined):
            return "Cannot migrate reference DB (config error), skipping."

        if "already exists" not in combined and "does not exist" not in combined:
            return "Cannot migrate reference DB (non-fakeable error), skipping."

        failing = _dslr.extract_failing_migration(stdout)
        if not failing:
            return "Cannot identify failing migration, skipping reference migration."

        app_label, migration_name = failing.split(".", 1)
        reason = "schema already exists" if "already exists" in combined else "table absent from dump"
        self.stdout.write(f"  Faking {failing} on reference DB ({reason})...\n")
        self._run_migrate(["manage.py", "migrate", app_label, migration_name, "--fake"], run_env)
        return ""

    # ------- restore + clone pipeline -------------------------------------

    def _restore_ref_and_copy(self, dump_path: str, label: str) -> bool:
        from teatree.utils.db import db_restore  # noqa: PLC0415

        cfg = self.cfg
        try:
            db_restore(cfg.ref_db_name, dump_path)
        except (RuntimeError, CommandFailedError) as exc:
            bad_artifacts.mark_bad(dump_path)
            self.stdout.write(f"  BAD ARTIFACT: {label} marked bad (delete: rm {dump_path})\n")
            self.stderr.write(f"    Restore error: {exc}\n")
            return False
        migrate_result = self._migrate_reference_db()
        if migrate_result is _MigrateResult.FAILED:
            bad_artifacts.mark_bad(dump_path)
            self.stdout.write(f"  BAD ARTIFACT: {label} marked bad (delete: rm {dump_path})\n")
            return False
        if self.dslr_cmd and migrate_result is _MigrateResult.APPLIED:
            self._take_dslr_snapshot()
        if self._copy_ref_to_ticket():
            self.stdout.write(f"  Created {cfg.ticket_db_name} from {label}.\n")
            return True
        return False

    # ------- strategy 1: explicit dump path -------------------------------

    def _try_restore_from_dump_path(self) -> bool:
        """Restore from an explicit dump file path (skip all auto-discovery)."""
        from teatree.utils.db import db_restore  # noqa: PLC0415

        dump = Path(self.cfg.dump_path)
        if not dump.is_file():
            self.stdout.write(f"  ERROR: Dump file not found: {dump}\n")
            return False
        self.stdout.write(f"  Restoring from explicit dump: {dump}\n")
        _ensure_ref_db(self.cfg.ref_db_name, self.pg_host, self.pg_user, self.pg_env)
        try:
            db_restore(self.cfg.ref_db_name, str(dump))
        except (RuntimeError, CommandFailedError):
            logger.exception("Restore failed for dump %s", dump)
            return False
        migrate_result = self._migrate_reference_db()
        if migrate_result is _MigrateResult.FAILED:
            return False
        if self.dslr_cmd and migrate_result is _MigrateResult.APPLIED:
            self._take_dslr_snapshot()
        return self._copy_ref_to_ticket()

    # ------- strategy 2: DSLR snapshot ------------------------------------

    def _resolve_dslr_snapshots(self) -> list[str]:
        if self.cfg.dslr_snapshot:
            return [self.cfg.dslr_snapshot]
        return _dslr.find_dslr_snapshots(self.dslr_cmd, self.dslr_env, self.cfg.ref_db_name)

    def _log_dslr_restore_failure(self, snap_name: str, *, is_env: bool, stderr: str) -> None:
        if is_env:
            self.stdout.write(f"  WARNING: DSLR restore failed (environment error, not marking bad): {snap_name}\n")
        else:
            bad_artifacts.mark_bad(_dslr.dslr_artifact_key(snap_name))
            self.stdout.write(
                f"  BAD ARTIFACT: DSLR snapshot '{snap_name}' marked bad (delete: dslr delete {snap_name})\n",
            )
        if stderr:
            logger.warning("DSLR restore stderr for %s: %s", snap_name, stderr)
            self.stdout.write(f"    Restore error: {stderr[:200]}\n")

    def _try_restore_from_dslr(self, *, skip_dslr: bool) -> bool:
        if skip_dslr:
            logger.info("DSLR restore skipped (skip_dslr=True)")
            return False
        if not self.dslr_cmd:
            logger.info("DSLR restore skipped (no snapshot tool configured)")
            return False
        _ensure_ref_db(self.cfg.ref_db_name, self.pg_host, self.pg_user, self.pg_env)
        snapshots = self._resolve_dslr_snapshots()
        if not snapshots:
            return False
        for snap_name in snapshots:
            self.stdout.write(f"  Restoring {self.cfg.ref_db_name} from DSLR snapshot: {snap_name}\n")
            ok, is_env, stderr = _dslr.restore_ref_from_dslr(self.dslr_cmd, self.dslr_env, snap_name)
            if not ok:
                self._log_dslr_restore_failure(snap_name, is_env=is_env, stderr=stderr)
                continue
            migrate_result = self._migrate_reference_db()
            if migrate_result is _MigrateResult.FAILED:
                self.stdout.write(
                    f"  WARNING: Migration failed after DSLR restore of {snap_name} (not marking snapshot bad)\n",
                )
                continue
            if migrate_result is _MigrateResult.APPLIED:
                self._take_dslr_snapshot()
            else:
                self.stdout.write("  Skipping DSLR snapshot (DB already migrated, snapshot is up to date).\n")
            if self._copy_ref_to_ticket():
                self.stdout.write(f"  Created {self.cfg.ticket_db_name} from DSLR snapshot.\n")
                return True
            self.stdout.write(f"  WARNING: Template copy after DSLR {snap_name} failed, trying older...\n")
        logger.warning("All DSLR snapshots failed for %s", self.cfg.ref_db_name)
        self.stdout.write("  WARNING: All DSLR snapshots failed. Trying local dump fallback...\n")
        return False

    # ------- strategy 3: local dump file ----------------------------------

    def _try_restore_from_local_dump(self) -> bool:
        cfg = self.cfg
        dump_dir = Path(cfg.dump_dir)
        if not dump_dir.is_dir():
            logger.info("Local dump dir %s does not exist", dump_dir)
            return False
        dumps = sorted(
            (p for p in dump_dir.glob(cfg.dump_glob) if validate_dump(p) and not bad_artifacts.is_bad(str(p))),
            key=lambda p: p.name,
            reverse=True,
        )
        if not dumps:
            for zd in dump_dir.glob(cfg.dump_glob):
                if zd.stat().st_size == 0:
                    self.stdout.write(f"  WARNING: Skipping 0-byte dump: {zd.name} (delete it)\n")
            return False
        for dump in dumps:
            self.stdout.write(f"  Restoring from local dump: {dump.name}\n")
            if self._restore_ref_and_copy(str(dump), f"local dump ({dump.name})"):
                return True
            self.stdout.write(f"  WARNING: Local dump {dump.name} failed, trying older...\n")
        logger.warning("All local dumps failed for %s", cfg.ref_db_name)
        self.stdout.write("  WARNING: All local dumps failed. Trying remote dump...\n")
        return False

    # ------- strategy 4: remote pg_dump -----------------------------------

    def _try_fetch_remote_dump(self) -> bool:
        """Fetch a fresh dump from the remote DB into dump_dir.

        Reached only when the caller passed ``allow_remote_dump=True``,
        which is set exclusively after a successful per-invocation
        interactive approval gate (``teatree.utils.approval``, #777). That
        gate — not a blanket env var — is the safety mechanism: an
        unattended agent cannot self-approve, so this path cannot run
        without a human's explicit per-run confirmation.
        """
        cfg = self.cfg
        if not cfg.remote_db_url:
            logger.info("Remote dump skipped (no remote_db_url configured)")
            return False
        if self._remote_dump_failed:
            self.stdout.write("  Skipping remote dump (already failed in this run).\n")
            return False
        dump_dir = Path(cfg.dump_dir)
        try:
            today = datetime.now(tz=UTC).strftime("%Y%m%d")
            dump_path = dump_dir / f"{today}_{cfg.ref_db_name}.pgsql"
            self.stdout.write(f"  Dumping from remote DB ({cfg.ref_db_name})...\n")
            dump_dir.mkdir(parents=True, exist_ok=True)
            # --no-owner --no-privileges mirrors the user's known-good
            # import_db_from_dev_env.sh (#777): the dump is portable across
            # the remote→local-superuser boundary, so the local ownership-
            # reassignment post-steps take over cleanly. -Fc keeps it the
            # deterministic custom format db_restore auto-detects.
            result = run_allowed_to_fail(
                [
                    "pg_dump",
                    "-Fc",
                    "--no-owner",
                    "--no-privileges",
                    "-f",
                    str(dump_path),
                    cfg.remote_db_url,
                ],
                timeout=cfg.dump_timeout,
                expected_codes=None,
            )
        except TimeoutExpired:
            self._remote_dump_failed = True
            self.stdout.write(f"  WARNING: pg_dump timed out after {cfg.dump_timeout}s.\n")
            return False
        if result.returncode == 0:
            size_mb = dump_path.stat().st_size / 1_000_000 if dump_path.exists() else 0
            self.stdout.write(f"  Saved {dump_path.name} ({size_mb:.0f}MB)\n")
            return True
        self._remote_dump_failed = True
        stderr_text = (result.stderr or "").strip()
        logger.warning("pg_dump failed (rc=%d) for %s: %s", result.returncode, cfg.ref_db_name, stderr_text)
        self.stdout.write(f"  WARNING: pg_dump failed: {stderr_text}\n")
        return False

    # ------- strategy 5: CI dump ------------------------------------------

    def _try_restore_from_ci_dump(self) -> bool:
        cfg = self.cfg
        ci_dumps = sorted(
            Path(cfg.main_repo_path).glob(cfg.ci_dump_glob),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not ci_dumps:
            return False
        ci_dump = ci_dumps[0]
        self.stdout.write(f"  Restoring from CI dump (last resort): {ci_dump.name}\n")
        if self._restore_ref_and_copy(str(ci_dump), f"CI dump ({ci_dump.name})"):
            return True
        logger.warning("CI dump restore failed for %s", cfg.ref_db_name)
        self.stdout.write("  WARNING: CI dump restore failed.\n")
        return False

    # ------- orchestration ------------------------------------------------

    def _warn_slow_path(self, label: str) -> None:
        """Warn prominently when a non-DSLR (slow) restore path executes."""
        self.stderr.write(f"  WARNING [SLOW PATH]: {label}\n")
        self.stderr.write("  DSLR snapshots are the expected fast path. This operation is significantly slower.\n")

    def run(
        self,
        *,
        skip_dslr: bool = False,
        slow_import: bool = False,
        allow_remote_dump: bool = False,
    ) -> bool:
        """Execute the import.  Returns True on success, False if no source was available."""
        cfg = self.cfg
        if (cfg.dump_path and self._try_restore_from_dump_path()) or (
            not cfg.dump_path and self._try_restore_from_dslr(skip_dslr=skip_dslr)
        ):
            return True

        if not slow_import:
            self.stderr.write(
                "\n  DSLR restore failed or unavailable. Non-DSLR fallbacks are disabled by default.\n"
                "  Non-DSLR paths (pg_restore, remote dump) take minutes instead of seconds.\n"
                "  To allow slow fallback paths, re-run with: --fresh-dump --user-authorized <user-id>\n"
                "  (the equivalent internal flag is `slow_import=True`)\n"
                f"  The snapshot warmer keeps {cfg.ref_db_name} current out-of-band (souliane/teatree#2949) —\n"
                "  run refresh_reference_snapshot(cfg) directly or wait for its next tick.\n",
            )
            return False

        self._warn_slow_path("Falling back to pg_restore from local dump file.")
        if self._try_restore_from_local_dump():
            return True

        if allow_remote_dump:
            self._warn_slow_path("Downloading fresh dump from remote database (pg_dump over network).")
            if self._try_fetch_remote_dump() and self._try_restore_from_local_dump():
                return True

        self._warn_slow_path("Trying CI dump as last resort (pg_restore).")
        if self._try_restore_from_ci_dump():
            return True

        dump_dir = Path(cfg.dump_dir)
        self.stdout.write(f"  ERROR: No database source available for '{cfg.ref_db_name}'.\n")
        self.stdout.write(f"  - No local DSLR snapshot for {cfg.ref_db_name}\n")
        self.stdout.write(f"  - No dump in {dump_dir}/\n")
        self.stdout.write(f"  - No CI dump matching {cfg.ci_dump_glob} in {cfg.main_repo_path}\n")
        self.stdout.write("\n")
        if cfg.remote_db_url:
            self.stdout.write(
                "  To fetch a fresh dump from the remote DB, re-run with `--fresh-dump --user-authorized <user-id>`.\n"
            )
        else:
            self.stdout.write("  Configure remote_db_url in DjangoDbImportConfig to enable remote dump fetching.\n")
        return False


# ---------------------------------------------------------------------------
# Public API — module-level functions delegating to DjangoDbImporter.
# ---------------------------------------------------------------------------


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
    """
    return DjangoDbImporter(cfg).run(
        skip_dslr=skip_dslr,
        slow_import=slow_import,
        allow_remote_dump=allow_remote_dump,
    )
