"""Tests for teatree.utils.django_db — pure / DSLR / Postgres helpers + dump validation.

Split verbatim from the former monolithic ``tests/test_django_db.py``
(souliane/teatree#443). No behavior change.
"""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.utils import django_db_dslr as dslr_mod
from teatree.utils import run as run_mod
from teatree.utils.django_db import _ensure_ref_db, _local_db_url, _pg_args, _terminate_connections, validate_dump
from teatree.utils.django_db_dslr import dslr_env as _dslr_env
from teatree.utils.django_db_dslr import dslr_snap_name as _dslr_snap_name
from teatree.utils.django_db_dslr import extract_failing_migration as _extract_failing_migration
from teatree.utils.django_db_dslr import find_dslr_cmd as _find_dslr_cmd
from teatree.utils.django_db_dslr import find_dslr_snapshots as _find_dslr_snapshots
from teatree.utils.django_db_dslr import restore_ref_from_dslr as _restore_ref_from_dslr

from ._shared import _fail_run, _make_importer, _ok_run

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestExtractFailingMigration:
    def test_finds_migration_name(self) -> None:
        stdout = "Applying myapp.0042_auto...\nOK\n"
        assert _extract_failing_migration(stdout) == "myapp.0042_auto"

    def test_returns_none_when_no_match(self) -> None:
        assert _extract_failing_migration("no migration here") is None


class TestExtractInconsistentHistory:
    """Parse Django's ``InconsistentMigrationHistory`` message verbatim.

    souliane/teatree#1038: a master renumber makes the snapshot's old-numbered
    record fail ``check_consistent_history`` with this exact sentence, fired
    BEFORE any ``Applying …`` line. The parser keys on the canonical wording in
    ``django/db/migrations/loader.py::check_consistent_history``.
    """

    def test_parses_applied_and_dependency(self) -> None:
        from teatree.utils.django_db_reconcile import extract_inconsistent_history  # noqa: PLC0415

        combined = (
            "django.db.migrations.exceptions.InconsistentMigrationHistory: "
            "Migration realtymodule.0096_remove_realty_participant_authorization is applied "
            "before its dependency loanrequestmodule.0257_move_participant_authorization_data "
            "on database 'default'.\n"
        )
        assert extract_inconsistent_history(combined) == (
            ("realtymodule", "0096_remove_realty_participant_authorization"),
            ("loanrequestmodule", "0257_move_participant_authorization_data"),
        )

    def test_returns_none_on_unrelated_error(self) -> None:
        from teatree.utils.django_db_reconcile import extract_inconsistent_history  # noqa: PLC0415

        assert extract_inconsistent_history("relation foo already exists") is None
        assert extract_inconsistent_history("Applying myapp.0001_initial...\n") is None


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
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda p: p)
        assert _find_dslr_cmd("dslr") == ["/custom/dslr"]

    def test_prefers_uv_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without DSLR_CMD, always uses uv run (avoids broken pyenv shims)."""
        monkeypatch.delenv("DSLR_CMD", raising=False)
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda p: p)
        assert _find_dslr_cmd("dslr") == ["uv", "run", "dslr"]

    def test_falls_back_to_uv_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DSLR_CMD", raising=False)
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda p: "uv" if p == "uv" else None)
        assert _find_dslr_cmd("dslr") == ["uv", "run", "dslr"]

    def test_returns_empty_when_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DSLR_CMD", raising=False)
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda _: None)
        assert _find_dslr_cmd("dslr") == []

    def test_ignores_main_repo_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dslr runs from the host project venv, not the target repo."""
        monkeypatch.delenv("DSLR_CMD", raising=False)
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda p: p)
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
