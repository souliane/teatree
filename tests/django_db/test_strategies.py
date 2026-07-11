"""Tests for teatree.utils.django_db — restore strategies (local / remote / CI dump).

Split verbatim from the former monolithic ``tests/test_django_db.py``
(souliane/teatree#443). No behavior change.
"""

import subprocess
from pathlib import Path

import pytest

from teatree.utils import run as run_mod
from teatree.utils.django_db import DjangoDbImporter
from teatree.utils.django_db import importer as mod

from ._shared import _fail_run, _make_importer, _ok_run

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
    """Remote pg_dump is reached only when ``allow_remote_dump=True`` (#777).

    The blanket ``T3_ALLOW_REMOTE_DUMP`` env gate is gone — the safety
    mechanism moved to a per-invocation interactive approval gate at the
    CLI boundary (``teatree.utils.approval``). An unattended agent cannot
    reach this method because it cannot satisfy that gate (no TTY). These
    tests therefore exercise the post-approval behaviour directly.
    """

    def test_skips_when_no_remote_url(self, tmp_path: Path) -> None:
        importer = _make_importer(tmp_path)
        assert importer._try_fetch_remote_dump() is False

    def test_skips_when_already_failed(self, tmp_path: Path) -> None:
        importer = _make_importer(tmp_path, dslr_cmd=[], remote_db_url="postgres://u:p@host/db")
        importer._remote_dump_failed = True
        assert importer._try_fetch_remote_dump() is False

    def test_handles_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".data").mkdir()
        importer = _make_importer(tmp_path, dslr_cmd=[], remote_db_url="postgres://u:p@host/db")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired("pg_dump", 1800)),
        )
        assert importer._try_fetch_remote_dump() is False

    def test_handles_pg_dump_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".data").mkdir()
        importer = _make_importer(tmp_path, dslr_cmd=[], remote_db_url="postgres://u:p@host/db")
        monkeypatch.setattr(run_mod.subprocess, "run", _fail_run)
        assert importer._try_fetch_remote_dump() is False

    def test_returns_true_after_successful_fetch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        importer = _make_importer(tmp_path, dslr_cmd=[], remote_db_url="postgres://u:p@host/db")

        def fake_run(args, **kw):
            if isinstance(args, list) and args[0] == "pg_dump":
                dump_path = args[args.index("-f") + 1]
                Path(dump_path).write_bytes(b"PGDMP")
            return _ok_run()

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        assert importer._try_fetch_remote_dump() is True

    def test_pg_dump_uses_no_owner_no_privileges(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """User script hardening: dump portable across the superuser boundary.

        The user's known-good ``import_db_from_dev_env.sh`` passes
        ``--no-owner --no-privileges`` so the local ownership-reassignment
        post-steps can take over cleanly. Preserve that.
        """
        (tmp_path / ".data").mkdir()
        importer = _make_importer(tmp_path, dslr_cmd=[], remote_db_url="postgres://u:p@host/db")
        captured: list[list[str]] = []

        def fake_run(args, **kw):
            if isinstance(args, list) and args[0] == "pg_dump":
                captured.append(args)
                Path(args[args.index("-f") + 1]).write_bytes(b"PGDMP")
            return _ok_run()

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        importer._try_fetch_remote_dump()
        assert captured, "pg_dump was not invoked"
        assert "--no-owner" in captured[0]
        assert "--no-privileges" in captured[0]


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
