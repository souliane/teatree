"""Template-clone the app DB into the test DB, re-cloning on migration drift (souliane/teatree#3326).

The Postgres CLIs (``psql``/``createdb``/``dropdb``) are the unstoppable external
here, so they are stubbed at the ``subprocess.run`` seam — the same seam the sibling
django_db tests stub. Everything above it (drift verdict, clone command shape,
fail-toward-re-clone bias, the orchestrator's outcome mapping) is exercised for real.
"""

import io
from subprocess import CompletedProcess

import pytest

from teatree.utils import run as run_mod
from teatree.utils.django_db import (
    TestDbCloneResult as CloneResult,  # aliased: a bare ``Test*`` name is collected as a test class
)
from teatree.utils.django_db import clone_app_db_to_test_db, migrations_drifted, prepare_test_db

_HOST, _USER, _ENV = "localhost", "u", {"PGPASSWORD": "pw"}


class FakePg:
    """Stub ``subprocess.run`` for the pg CLIs, keyed on the command and target DB."""

    def __init__(
        self,
        migrations: dict[str, list[str] | None],
        *,
        dropdb_ok: bool = True,
        createdb_ok: bool = True,
    ) -> None:
        # db name -> its django_migrations rows as "app|name" lines, or None = unreadable.
        self.migrations = migrations
        self.dropdb_ok = dropdb_ok
        self.createdb_ok = createdb_ok
        self.commands: list[list[str]] = []

    def __call__(self, args: list[str], **_kwargs: object) -> CompletedProcess[str]:
        self.commands.append(args)
        tool = args[0]
        if tool == "psql" and "-tAqc" in args:
            db = args[args.index("-d") + 1]
            rows = self.migrations.get(db)
            if rows is None:
                return CompletedProcess(args, 1, "", f'database "{db}" does not exist')
            return CompletedProcess(args, 0, "".join(f"{row}\n" for row in rows), "")
        if tool == "dropdb":
            return CompletedProcess(args, 0 if self.dropdb_ok else 1, "", "")
        if tool == "createdb":
            return CompletedProcess(args, 0 if self.createdb_ok else 1, "", "")
        return CompletedProcess(args, 0, "", "")  # psql terminate-connections

    def tools(self) -> list[str]:
        return [cmd[0] for cmd in self.commands]


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakePg) -> FakePg:
    monkeypatch.setattr(run_mod.subprocess, "run", fake)
    return fake


def _drifted() -> bool:
    return migrations_drifted("app", "test", host=_HOST, user=_USER, env=_ENV)


class TestMigrationsDrifted:
    def test_identical_signatures_are_not_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = ["app|0001_initial", "app|0002_thing"]
        _install(monkeypatch, FakePg({"app": rows, "test": list(reversed(rows))}))
        assert _drifted() is False  # order-independent (compared as a set)

    def test_different_applied_sets_are_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakePg({"app": ["app|0001", "app|0002"], "test": ["app|0001"]}))
        assert _drifted() is True

    def test_unreadable_test_db_reads_as_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Missing/erroring test DB must never be mistaken for "in sync".
        _install(monkeypatch, FakePg({"app": ["app|0001"], "test": None}))
        assert _drifted() is True

    def test_unreadable_app_db_reads_as_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakePg({"app": None, "test": ["app|0001"]}))
        assert _drifted() is True

    def test_malformed_row_reads_as_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A row without the expected "app|name" shape is uncertainty, not a match.
        _install(monkeypatch, FakePg({"app": ["app|0001"], "test": ["garbage-no-separator"]}))
        assert _drifted() is True

    def test_blank_lines_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakePg({"app": ["app|0001", ""], "test": ["app|0001"]}))
        assert _drifted() is False


class TestCloneAppDbToTestDb:
    def _clone(self) -> bool:
        return clone_app_db_to_test_db(app_db="app", test_db="test", host=_HOST, user=_USER, env=_ENV)

    def test_success_issues_forced_drop_then_template_createdb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install(monkeypatch, FakePg({}))
        assert self._clone() is True
        assert fake.tools() == ["dropdb", "psql", "createdb"]  # drop → terminate template conns → createdb -T
        drop = fake.commands[0]
        assert "--force" in drop  # PG 13+ atomic connection-terminate before drop
        assert "--if-exists" in drop
        assert drop[-1] == "test"
        createdb = fake.commands[-1]
        assert createdb[-2:] == ["-T", "app"]

    def test_failed_dropdb_aborts_before_createdb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install(monkeypatch, FakePg({}, dropdb_ok=False))
        assert self._clone() is False
        assert "createdb" not in fake.tools()

    def test_failed_createdb_reports_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakePg({}, createdb_ok=False))
        assert self._clone() is False


class TestPrepareTestDb:
    """``prepare_test_db`` resolves the pg connection from the environment (subprocess faked)."""

    def _prepare(self, monkeypatch: pytest.MonkeyPatch) -> tuple[CloneResult, str]:
        # A concrete env so pg_host/user/env resolve deterministically; the subprocess
        # is faked, so these values only shape the (unasserted) command, not behaviour.
        monkeypatch.setenv("POSTGRES_HOST", "db.local")
        monkeypatch.setenv("POSTGRES_USER", "importer")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
        out = io.StringIO()
        return prepare_test_db(app_db="app", test_db="test", stdout=out), out.getvalue()

    def test_in_sync_reuses_without_cloning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install(monkeypatch, FakePg({"app": ["app|0001"], "test": ["app|0001"]}))
        result, _ = self._prepare(monkeypatch)
        assert result is CloneResult.REUSED
        assert "createdb" not in fake.tools()  # no clone when the DB is already current

    def test_drift_reclones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install(monkeypatch, FakePg({"app": ["app|0001", "app|0002"], "test": ["app|0001"]}))
        result, _ = self._prepare(monkeypatch)
        assert result is CloneResult.RECLONED
        assert "createdb" in fake.tools()

    def test_clone_failure_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakePg({"app": ["app|0001"], "test": None}, createdb_ok=False))
        result, message = self._prepare(monkeypatch)
        assert result is CloneResult.FAILED
        assert "replay" in message


class TestIsCurrent:
    def test_reused_and_recloned_are_current(self) -> None:
        assert CloneResult.REUSED.is_current
        assert CloneResult.RECLONED.is_current

    def test_failed_is_not_current(self) -> None:
        assert not CloneResult.FAILED.is_current
