"""Tests for teatree.utils.django_db — restore-and-copy pipeline + DSLR restore.

Split verbatim from the former monolithic ``tests/test_django_db.py``
(souliane/teatree#443). No behavior change.
"""

from pathlib import Path

import pytest

from teatree.utils import run as run_mod
from teatree.utils.django_db import DjangoDbImporter
from teatree.utils.django_db import dslr as dslr_mod
from teatree.utils.django_db.migrate import _MigrateResult

from ._shared import _make_importer, _ok_run


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
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *a: [])
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is False

    def test_succeeds_with_snapshot_and_runs_migrate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *a: ["20260326_development-acme"])
        monkeypatch.setattr(dslr_mod, "restore_ref_from_dslr", lambda *a: (True, False, ""))
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.APPLIED)
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: None)
        monkeypatch.setattr(DjangoDbImporter, "_copy_ref_to_ticket", lambda self: True)
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is True

    def test_skips_dslr_snapshot_when_already_migrated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        snapshot_calls: list[str] = []
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *a: ["20260326_development-acme"])
        monkeypatch.setattr(dslr_mod, "restore_ref_from_dslr", lambda *a: (True, False, ""))
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
            dslr_mod,
            "find_dslr_snapshots",
            lambda *a: ["20260326_development-acme", "20260320_development-acme"],
        )
        monkeypatch.setattr(dslr_mod, "restore_ref_from_dslr", fake_restore)
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
            dslr_mod,
            "find_dslr_snapshots",
            lambda *a: ["20260326_development-acme", "20260320_development-acme"],
        )
        monkeypatch.setattr(dslr_mod, "restore_ref_from_dslr", lambda *a: (True, False, ""))
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", fake_migrate)
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: None)
        monkeypatch.setattr(DjangoDbImporter, "_copy_ref_to_ticket", lambda self: True)
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is True
        assert len(migrate_calls) == 2

    def test_falls_back_when_all_snapshots_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            dslr_mod,
            "find_dslr_snapshots",
            lambda *a: ["20260326_development-acme", "20260320_development-acme"],
        )
        monkeypatch.setattr(dslr_mod, "restore_ref_from_dslr", lambda *a: (False, False, "mock error"))
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is False

    def test_falls_back_when_template_copy_fails_after_restore(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *a: ["20260326_development-acme"])
        monkeypatch.setattr(dslr_mod, "restore_ref_from_dslr", lambda *a: (True, False, ""))
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.APPLIED)
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: None)
        monkeypatch.setattr(DjangoDbImporter, "_copy_ref_to_ticket", lambda self: False)
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        importer = _make_importer(tmp_path)
        assert importer._try_restore_from_dslr(skip_dslr=False) is False
