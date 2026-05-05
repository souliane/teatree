"""Tests for teatree.utils.django_db — generic Django DB import engine."""

import io
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.utils import bad_artifacts
from teatree.utils import django_db as mod
from teatree.utils import run as run_mod
from teatree.utils.django_db import (
    DjangoDbImportConfig,
    DjangoDbImporter,
    _dslr_env,
    _dslr_snap_name,
    _ensure_ref_db,
    _extract_failing_migration,
    _find_dslr_cmd,
    _find_dslr_snapshots,
    _local_db_url,
    _MigrateResult,
    _parse_dslr_snapshots,
    _pg_args,
    _restore_ref_from_dslr,
    _terminate_connections,
    django_db_import,
    prune_dslr_snapshots,
    validate_dump,
)


@pytest.fixture(autouse=True)
def _isolate_bad_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bad_artifacts, "_CACHE_FILE", tmp_path / "bad_artifacts.json")


def _make_cfg(tmp_path: Path, **overrides: str) -> DjangoDbImportConfig:
    defaults = {
        "ref_db_name": "development-acme",
        "ticket_db_name": "wt_42_acme",
        "main_repo_path": str(tmp_path),
        "dump_dir": str(tmp_path / ".data"),
        "dump_glob": "*development-acme*.pgsql",
        "ci_dump_glob": ".gitlab/dump_after_migration.*.sql.gz",
    }
    defaults.update(overrides)
    return DjangoDbImportConfig(**defaults)


def _make_importer(tmp_path: Path, *, dslr_cmd: list[str] | None = None, **cfg_overrides: str) -> DjangoDbImporter:
    """Build an importer with stubbed stdout/stderr and a controllable dslr command."""
    importer = DjangoDbImporter(_make_cfg(tmp_path, **cfg_overrides), stdout=io.StringIO(), stderr=io.StringIO())
    importer.dslr_cmd = dslr_cmd if dslr_cmd is not None else ["/usr/bin/dslr"]
    importer.dslr_env = {"DATABASE_URL": "postgres://u:p@localhost/dev"} if importer.dslr_cmd else {}
    importer.pg_host = "localhost"
    importer.pg_user = "local_superuser"
    importer.pg_env = {"PGPASSWORD": "pw"}
    return importer


def _ok_run(*_args, **_kwargs) -> CompletedProcess:
    return CompletedProcess(args=_args, returncode=0, stdout="", stderr="")


def _fail_run(*_args, **_kwargs) -> CompletedProcess:
    return CompletedProcess(args=_args, returncode=1, stdout="", stderr="error")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestExtractFailingMigration:
    def test_finds_migration_name(self) -> None:
        stdout = "Applying myapp.0042_auto...\nOK\n"
        assert _extract_failing_migration(stdout) == "myapp.0042_auto"

    def test_returns_none_when_no_match(self) -> None:
        assert _extract_failing_migration("no migration here") is None


class TestDslrSnapName:
    def test_includes_ref_db_name(self) -> None:
        result = _dslr_snap_name("development-acme")
        assert result.endswith("_development-acme")
        assert len(result.split("_")[0]) == 8  # YYYYMMDD


class TestLocalDbUrl:
    def test_builds_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_HOST", "db.local")
        monkeypatch.setenv("POSTGRES_USER", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss")
        url = _local_db_url("mydb")
        assert "db.local" in url
        assert "mydb" in url
        assert "p%40ss" in url  # URL-encoded


class TestPgArgs:
    def test_reads_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_HOST", "h")
        monkeypatch.setenv("POSTGRES_USER", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        host, user, env = _pg_args()
        assert host == "h"
        assert user == "u"
        assert env["PGPASSWORD"] == "p"
        assert env["PGPORT"] == "5433"

    def test_defaults_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POSTGRES_HOST", raising=False)
        monkeypatch.delenv("POSTGRES_USER", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        monkeypatch.delenv("POSTGRES_PORT", raising=False)
        host, user, _env = _pg_args()
        assert host == "localhost"
        assert user == "postgres"  # default from db.pg_user()


# ---------------------------------------------------------------------------
# DSLR helpers
# ---------------------------------------------------------------------------


class TestFindDslrCmd:
    def test_uses_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DSLR_CMD", "/custom/dslr")
        monkeypatch.setattr(mod.shutil, "which", lambda p: p)
        assert _find_dslr_cmd("dslr") == ["/custom/dslr"]

    def test_prefers_uv_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without DSLR_CMD, always uses uv run (avoids broken pyenv shims)."""
        monkeypatch.delenv("DSLR_CMD", raising=False)
        monkeypatch.setattr(mod.shutil, "which", lambda p: p)
        assert _find_dslr_cmd("dslr") == ["uv", "run", "dslr"]

    def test_falls_back_to_uv_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DSLR_CMD", raising=False)
        monkeypatch.setattr(mod.shutil, "which", lambda p: "uv" if p == "uv" else None)
        assert _find_dslr_cmd("dslr") == ["uv", "run", "dslr"]

    def test_returns_empty_when_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DSLR_CMD", raising=False)
        monkeypatch.setattr(mod.shutil, "which", lambda _: None)
        assert _find_dslr_cmd("dslr") == []

    def test_ignores_main_repo_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dslr runs from the host project venv, not the target repo."""
        monkeypatch.delenv("DSLR_CMD", raising=False)
        monkeypatch.setattr(mod.shutil, "which", lambda p: p)
        assert _find_dslr_cmd("dslr", "/repo/main") == ["uv", "run", "dslr"]


class TestDslrEnv:
    def test_sets_database_urls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_HOST", "localhost")
        monkeypatch.setenv("POSTGRES_USER", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        env = _dslr_env("development-acme")
        assert "development-acme" in env["DATABASE_URL"]
        assert env["DSLR_DB_URL"] == env["DATABASE_URL"]


class TestFindDslrSnapshots:
    def test_returns_sorted_newest_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = (
            "20260301_development-acme  2026-03-01  100MB\n"
            "20260315_development-acme  2026-03-15  110MB\n"
            "20260310_development-other  2026-03-10  90MB\n"
        )
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 0, output, ""),
        )
        result = _find_dslr_snapshots(["/bin/dslr"], {}, "development-acme")
        assert result == ["20260315_development-acme", "20260301_development-acme"]

    def test_returns_empty_when_no_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 0, "", ""),
        )
        assert _find_dslr_snapshots(["/bin/dslr"], {}, "development-acme") == []

    def test_returns_empty_when_command_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "", "error"),
        )
        assert _find_dslr_snapshots(["/bin/dslr"], {}, "development-acme") == []


class TestRestoreRefFromDslr:
    def test_returns_success_tuple_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        assert _restore_ref_from_dslr(["/bin/dslr"], {}, "snap1") == (True, False, "")

    def test_returns_failure_tuple_with_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_mod.subprocess, "run", _fail_run)
        ok, _is_env, stderr = _restore_ref_from_dslr(["/bin/dslr"], {}, "snap1")
        assert ok is False
        assert isinstance(stderr, str)


class TestTakeDslrSnapshot:
    def test_calls_dslr_snapshot(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        commands: list = []
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda args, **kw: commands.append(args) or _ok_run(),
        )
        importer = _make_importer(tmp_path, dslr_cmd=["/bin/dslr"])
        importer._take_dslr_snapshot()
        assert commands[0][0] == "/bin/dslr"
        assert commands[0][1] == "snapshot"


# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------


class TestEnsureRefDb:
    def test_calls_createdb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        commands: list = []
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda args, **kw: commands.append(args) or _ok_run(),
        )
        _ensure_ref_db("development-acme", "localhost", "u", {})
        assert commands[0][0] == "createdb"
        assert "development-acme" in commands[0]


class TestTerminateConnections:
    def test_calls_psql(self, monkeypatch: pytest.MonkeyPatch) -> None:
        commands: list = []
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda args, **kw: commands.append(args) or _ok_run(),
        )
        _terminate_connections("mydb", "localhost", "u", {})
        assert commands[0][0] == "psql"


class TestCopyRefToTicket:
    def test_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._copy_ref_to_ticket() is True

    def test_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "", "template copy error"),
        )
        importer = _make_importer(tmp_path)
        assert importer._copy_ref_to_ticket() is False


# ---------------------------------------------------------------------------
# Dump validation
# ---------------------------------------------------------------------------


class TestValidateDump:
    def test_rejects_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.pgsql"
        f.write_bytes(b"")
        assert validate_dump(f) is False

    def test_rejects_truncated_dump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "trunc.pgsql"
        f.write_bytes(b"some data")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "", "could not read"),
        )
        assert validate_dump(f) is False

    def test_accepts_valid_dump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "ok.pgsql"
        f.write_bytes(b"PGDMP data here")
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        assert validate_dump(f) is True


# ---------------------------------------------------------------------------
# Migration with selective faking
# ---------------------------------------------------------------------------


class TestMigrateReferenceDb:
    def test_succeeds_on_first_try(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED

    def test_returns_already_migrated_when_no_migrations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 0, "No migrations to apply.\n", ""),
        )
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.ALREADY_MIGRATED

    def test_fakes_already_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        calls: list[list[str]] = []
        call_count = 0

        def fake_run(args, **kw):
            nonlocal call_count
            calls.append(list(args))
            call_count += 1
            if call_count == 1:
                return CompletedProcess(args, 1, "Applying myapp.0005_add_field...\n", "already exists")
            return CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED
        assert "--fake" in calls[1]

    def test_skips_on_config_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "", "ModuleNotFoundError: No module named 'foo'"),
        )
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.FAILED

    def test_skips_on_non_fakeable_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "", "unexpected error"),
        )
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.FAILED

    def test_skips_when_no_manage_py(self, tmp_path: Path) -> None:
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.ALREADY_MIGRATED

    def test_skips_when_failing_migration_not_parseable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "Running migrate...\n", "already exists"),
        )
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.FAILED

    def test_exhausts_retries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "Applying myapp.0001_init...\n", "already exists"),
        )
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.FAILED

    def test_fakes_does_not_exist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        calls: list[list[str]] = []
        call_count = 0

        def fake_run(args, **kw):
            nonlocal call_count
            calls.append(list(args))
            call_count += 1
            if call_count == 1:
                return CompletedProcess(args, 1, "Applying myapp.0005_drop...\n", "does not exist")
            return CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED


# ---------------------------------------------------------------------------
# Restore-and-copy pipeline
# ---------------------------------------------------------------------------


class TestRestoreRefAndCopy:
    def test_success_with_dslr(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        monkeypatch.setattr("teatree.utils.db.db_restore", lambda *a, **kw: None)
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        importer = _make_importer(tmp_path)
        assert importer._restore_ref_and_copy("/tmp/dump.pgsql", "test") is True

    def test_failure_on_restore(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.utils.db.db_restore", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        importer = _make_importer(tmp_path)
        assert importer._restore_ref_and_copy("/tmp/dump.pgsql", "test") is False

    def test_success_without_dslr(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        monkeypatch.setattr("teatree.utils.db.db_restore", lambda *a, **kw: None)
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        importer = _make_importer(tmp_path, dslr_cmd=[])
        assert importer._restore_ref_and_copy("/tmp/dump.pgsql", "test") is True

    def test_returns_false_when_migration_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.utils.db.db_restore", lambda *a, **kw: None)
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.FAILED)
        importer = _make_importer(tmp_path)
        assert importer._restore_ref_and_copy("/tmp/dump.pgsql", "test") is False

    def test_returns_false_when_template_copy_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.utils.db.db_restore", lambda *a, **kw: None)
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.APPLIED)
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: None)
        monkeypatch.setattr(DjangoDbImporter, "_copy_ref_to_ticket", lambda self: False)
        importer = _make_importer(tmp_path)
        assert importer._restore_ref_and_copy("/tmp/dump.pgsql", "test") is False


class TestTryRestoreFromDslr:
    def test_skips_when_no_dslr(self, tmp_path: Path) -> None:
        importer = _make_importer(tmp_path, dslr_cmd=[])
        assert importer._try_restore_from_dslr(skip_dslr=False) is False

    def test_skips_when_skip_flag(self, tmp_path: Path) -> None:
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=True) is False

    def test_skips_when_no_snapshots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "_find_dslr_snapshots", lambda *a: [])
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is False

    def test_succeeds_with_snapshot_and_runs_migrate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "_find_dslr_snapshots", lambda *a: ["20260326_development-acme"])
        monkeypatch.setattr(mod, "_restore_ref_from_dslr", lambda *a: (True, False, ""))
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.APPLIED)
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: None)
        monkeypatch.setattr(DjangoDbImporter, "_copy_ref_to_ticket", lambda self: True)
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is True

    def test_skips_dslr_snapshot_when_already_migrated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        snapshot_calls: list[str] = []
        monkeypatch.setattr(mod, "_find_dslr_snapshots", lambda *a: ["20260326_development-acme"])
        monkeypatch.setattr(mod, "_restore_ref_from_dslr", lambda *a: (True, False, ""))
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.ALREADY_MIGRATED)
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: snapshot_calls.append("called"))
        monkeypatch.setattr(DjangoDbImporter, "_copy_ref_to_ticket", lambda self: True)
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is True
        assert snapshot_calls == [], "DSLR snapshot should be skipped when DB is already migrated"

    def test_tries_older_snapshot_when_first_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts: list[str] = []

        def fake_restore(_cmd, _env, snap):
            attempts.append(snap)
            ok = snap != "20260326_development-acme"
            return (ok, False, "" if ok else "mock restore error")

        monkeypatch.setattr(
            mod, "_find_dslr_snapshots", lambda *a: ["20260326_development-acme", "20260320_development-acme"]
        )
        monkeypatch.setattr(mod, "_restore_ref_from_dslr", fake_restore)
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.APPLIED)
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: None)
        monkeypatch.setattr(DjangoDbImporter, "_copy_ref_to_ticket", lambda self: True)
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is True
        assert attempts == ["20260326_development-acme", "20260320_development-acme"]

    def test_tries_older_snapshot_when_migration_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        migrate_calls: list[None] = []

        def fake_migrate(_self):
            migrate_calls.append(None)
            return _MigrateResult.APPLIED if len(migrate_calls) == 2 else _MigrateResult.FAILED

        monkeypatch.setattr(
            mod, "_find_dslr_snapshots", lambda *a: ["20260326_development-acme", "20260320_development-acme"]
        )
        monkeypatch.setattr(mod, "_restore_ref_from_dslr", lambda *a: (True, False, ""))
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", fake_migrate)
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: None)
        monkeypatch.setattr(DjangoDbImporter, "_copy_ref_to_ticket", lambda self: True)
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is True
        assert len(migrate_calls) == 2

    def test_falls_back_when_all_snapshots_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            mod, "_find_dslr_snapshots", lambda *a: ["20260326_development-acme", "20260320_development-acme"]
        )
        monkeypatch.setattr(mod, "_restore_ref_from_dslr", lambda *a: (False, False, "mock error"))
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is False

    def test_falls_back_when_template_copy_fails_after_restore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "_find_dslr_snapshots", lambda *a: ["20260326_development-acme"])
        monkeypatch.setattr(mod, "_restore_ref_from_dslr", lambda *a: (True, False, ""))
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.APPLIED)
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: None)
        monkeypatch.setattr(DjangoDbImporter, "_copy_ref_to_ticket", lambda self: False)
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is False


# ---------------------------------------------------------------------------
# Strategy: local dump
# ---------------------------------------------------------------------------


class TestTryRestoreFromLocalDump:
    def test_skips_when_no_dump_dir(self, tmp_path: Path) -> None:
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_local_dump() is False

    def test_skips_when_no_matching_dumps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".data").mkdir()
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_local_dump() is False

    def test_warns_about_zero_byte_dumps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        (data_dir / "20260101_development-acme.pgsql").write_bytes(b"")
        # Also add a non-zero but truncated dump (filtered by validate_dump)
        (data_dir / "20260102_development-acme.pgsql").write_bytes(b"truncated")
        monkeypatch.setattr(mod, "validate_dump", lambda p: False)
        importer = _make_importer(tmp_path)
        importer._try_restore_from_local_dump()
        assert "0-byte" in importer.stdout.getvalue()

    def test_succeeds_with_valid_dump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        (data_dir / "20260301_development-acme.pgsql").write_bytes(b"PGDMP")
        monkeypatch.setattr(mod, "validate_dump", lambda p: True)
        monkeypatch.setattr(DjangoDbImporter, "_restore_ref_and_copy", lambda self, path, label: True)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_local_dump() is True

    def test_tries_older_dump_when_first_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        (data_dir / "20260301_development-acme.pgsql").write_bytes(b"PGDMP")
        (data_dir / "20260215_development-acme.pgsql").write_bytes(b"PGDMP")
        attempted: list[str] = []

        def fake_restore(_self, path, _label):
            attempted.append(Path(path).name)
            return "20260215" in path

        monkeypatch.setattr(mod, "validate_dump", lambda p: True)
        monkeypatch.setattr(DjangoDbImporter, "_restore_ref_and_copy", fake_restore)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_local_dump() is True
        assert len(attempted) == 2

    def test_falls_back_when_all_dumps_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        (data_dir / "20260301_development-acme.pgsql").write_bytes(b"PGDMP")
        monkeypatch.setattr(mod, "validate_dump", lambda p: True)
        monkeypatch.setattr(DjangoDbImporter, "_restore_ref_and_copy", lambda self, path, label: False)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_local_dump() is False


# ---------------------------------------------------------------------------
# Strategy: remote dump
# ---------------------------------------------------------------------------


class TestTryFetchRemoteDump:
    def test_blocks_when_env_gate_not_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard: even with remote_url configured, the env gate must block.

        2026-04-20 incident: agent-triggered lifecycle path auto-fetched gigabyte
        dumps over VPN. The env gate is the final safety net.
        """
        monkeypatch.delenv("T3_ALLOW_REMOTE_DUMP", raising=False)
        importer = _make_importer(tmp_path, dslr_cmd=[], remote_db_url="postgres://u:p@host/db")
        assert importer._try_fetch_remote_dump() is False

    def test_skips_when_no_remote_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_ALLOW_REMOTE_DUMP", "1")
        importer = _make_importer(tmp_path)
        assert importer._try_fetch_remote_dump() is False

    def test_skips_when_already_failed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_ALLOW_REMOTE_DUMP", "1")
        importer = _make_importer(tmp_path, dslr_cmd=[], remote_db_url="postgres://u:p@host/db")
        importer._remote_dump_failed = True
        assert importer._try_fetch_remote_dump() is False

    def test_handles_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_ALLOW_REMOTE_DUMP", "1")
        (tmp_path / ".data").mkdir()
        importer = _make_importer(tmp_path, dslr_cmd=[], remote_db_url="postgres://u:p@host/db")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired("pg_dump", 1800)),
        )
        assert importer._try_fetch_remote_dump() is False

    def test_handles_pg_dump_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_ALLOW_REMOTE_DUMP", "1")
        (tmp_path / ".data").mkdir()
        importer = _make_importer(tmp_path, dslr_cmd=[], remote_db_url="postgres://u:p@host/db")
        monkeypatch.setattr(run_mod.subprocess, "run", _fail_run)
        assert importer._try_fetch_remote_dump() is False

    def test_returns_true_after_successful_fetch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_ALLOW_REMOTE_DUMP", "1")
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        importer = _make_importer(tmp_path, dslr_cmd=[], remote_db_url="postgres://u:p@host/db")

        def fake_run(args, **kw):
            if isinstance(args, list) and args[0] == "pg_dump":
                dump_path = args[3]
                Path(dump_path).write_bytes(b"PGDMP")
            return _ok_run()

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        assert importer._try_fetch_remote_dump() is True


# ---------------------------------------------------------------------------
# Strategy: CI dump
# ---------------------------------------------------------------------------


class TestTryRestoreFromCiDump:
    def test_skips_when_no_ci_dumps(self, tmp_path: Path) -> None:
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_ci_dump() is False

    def test_succeeds_with_ci_dump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ci_dir = tmp_path / ".gitlab"
        ci_dir.mkdir()
        (ci_dir / "dump_after_migration.20260301.sql.gz").write_bytes(b"data")
        monkeypatch.setattr(DjangoDbImporter, "_restore_ref_and_copy", lambda self, path, label: True)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_ci_dump() is True

    def test_falls_back_when_restore_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ci_dir = tmp_path / ".gitlab"
        ci_dir.mkdir()
        (ci_dir / "dump_after_migration.20260301.sql.gz").write_bytes(b"data")
        monkeypatch.setattr(DjangoDbImporter, "_restore_ref_and_copy", lambda self, path, label: False)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_ci_dump() is False


# ---------------------------------------------------------------------------
# Full orchestration
# ---------------------------------------------------------------------------


class TestDjangoDbImport:
    def test_succeeds_via_dslr(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "_find_dslr_cmd", lambda *a, **kw: ["/bin/dslr"])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg) is True

    def test_falls_through_to_local_dump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "_find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg, slow_import=True) is True

    def test_blocks_fallback_without_slow_import(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-DSLR fallbacks require --slow-import."""
        monkeypatch.setattr(mod, "_find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg) is False

    def test_falls_through_to_remote_fetch_then_local(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        local_calls: list[int] = []
        monkeypatch.setattr(mod, "_find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)

        def local_dump(_self):
            local_calls.append(1)
            return len(local_calls) == 2  # fail first, succeed after remote fetch

        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", local_dump)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg, slow_import=True, allow_remote_dump=True) is True
        assert len(local_calls) == 2  # called twice: before and after remote fetch

    def test_skips_remote_when_not_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        remote_called: list[int] = []
        monkeypatch.setattr(mod, "_find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: remote_called.append(1) or True)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_ci_dump", lambda self: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg, slow_import=True, allow_remote_dump=False) is True
        assert remote_called == []

    def test_falls_through_to_ci(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "_find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_ci_dump", lambda self: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg, slow_import=True) is True

    def test_fails_when_all_strategies_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "_find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_ci_dump", lambda self: False)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg, slow_import=True) is False

    def test_failure_message_with_remote_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(mod, "_find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_ci_dump", lambda self: False)
        cfg = _make_cfg(tmp_path, remote_db_url="postgres://u:p@host/db")
        django_db_import(cfg, slow_import=True)
        assert "--slow-import" in capsys.readouterr().out

    def test_failure_message_without_remote_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(mod, "_find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_ci_dump", lambda self: False)
        cfg = _make_cfg(tmp_path)
        django_db_import(cfg, slow_import=True)
        assert "Configure remote_db_url" in capsys.readouterr().out

    def test_skip_dslr_passed_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured_skip: list[bool] = []
        monkeypatch.setattr(mod, "_find_dslr_cmd", lambda *a, **kw: ["/bin/dslr"])

        def capture_dslr(_self, *, skip_dslr):
            captured_skip.append(skip_dslr)
            return True

        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", capture_dslr)
        cfg = _make_cfg(tmp_path)
        django_db_import(cfg, skip_dslr=True)
        assert captured_skip == [True]

    def test_no_snapshot_tool_skips_dslr_setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: True)
        cfg = DjangoDbImportConfig(
            ref_db_name="development-acme",
            ticket_db_name="wt_42_acme",
            main_repo_path=str(tmp_path),
            dump_dir=str(tmp_path / ".data"),
            dump_glob="*.pgsql",
            ci_dump_glob="*.sql.gz",
            snapshot_tool="",
        )
        assert django_db_import(cfg, slow_import=True) is True


# ---------------------------------------------------------------------------
# DSLR snapshot pruning
# ---------------------------------------------------------------------------


class TestParseDslrSnapshots:
    def test_groups_by_tenant(self) -> None:
        stdout = (
            "20260402_development-acme  125MB\n"
            "20260401_development-acme  123MB\n"
            "20260315_development-volksbank  98MB\n"
            "20260320_development-volksbank  100MB\n"
        )
        result = _parse_dslr_snapshots(stdout)
        assert set(result) == {"development-acme", "development-volksbank"}
        assert result["development-acme"] == [
            "20260402_development-acme",
            "20260401_development-acme",
        ]
        assert result["development-volksbank"] == [
            "20260320_development-volksbank",
            "20260315_development-volksbank",
        ]

    def test_empty_output(self) -> None:
        assert _parse_dslr_snapshots("") == {}

    def test_skips_blank_lines(self) -> None:
        result = _parse_dslr_snapshots("\n\n20260401_dev-acme  50MB\n\n")
        assert result == {"dev-acme": ["20260401_dev-acme"]}


class TestPruneDslrSnapshots:
    def test_deletes_old_keeps_newest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dslr_output = (
            "20260402_development-acme  125MB\n20260401_development-acme  123MB\n20260320_development-acme  120MB\n"
        )
        deleted: list[str] = []

        def fake_run(cmd, **kw):
            if "delete" in cmd:
                deleted.append(cmd[-1])
            return CompletedProcess(cmd, 0, stdout=dslr_output, stderr="")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/uv")
        result = prune_dslr_snapshots(keep=1)

        assert result == ["20260401_development-acme", "20260320_development-acme"]
        assert deleted == ["20260401_development-acme", "20260320_development-acme"]

    def test_keeps_n_snapshots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dslr_output = "20260403_dev-a  10MB\n20260402_dev-a  10MB\n20260401_dev-a  10MB\n"
        deleted: list[str] = []

        def fake_run(cmd, **kw):
            if "delete" in cmd:
                deleted.append(cmd[-1])
            return CompletedProcess(cmd, 0, stdout=dslr_output, stderr="")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/uv")
        result = prune_dslr_snapshots(keep=2)

        assert result == ["20260401_dev-a"]

    def test_returns_empty_when_no_dslr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod.shutil, "which", lambda _: None)
        monkeypatch.delenv("DSLR_CMD", raising=False)
        assert prune_dslr_snapshots() == []

    def test_returns_empty_when_dslr_list_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_mod.subprocess, "run", lambda *a, **kw: CompletedProcess(a, 1, stdout="", stderr=""))
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/uv")
        assert prune_dslr_snapshots() == []
