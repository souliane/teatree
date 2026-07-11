"""Tests for teatree.utils.django_db — full import orchestration.

Split verbatim from the former monolithic ``tests/test_django_db.py``
(souliane/teatree#443). No behavior change.
"""

from pathlib import Path

import pytest

from teatree.utils.django_db import DjangoDbImportConfig, DjangoDbImporter, django_db_import
from teatree.utils.django_db import dslr as dslr_mod

from ._shared import _make_cfg


class TestDjangoDbImport:
    def test_succeeds_via_dslr(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *a, **kw: ["/bin/dslr"])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg) is True

    def test_falls_through_to_local_dump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg, slow_import=True) is True

    def test_blocks_fallback_without_slow_import(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-DSLR fallbacks require --slow-import."""
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg) is False

    def test_falls_through_to_remote_fetch_then_local(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        local_calls: list[int] = []
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *a, **kw: [])
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
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: remote_called.append(1) or True)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_ci_dump", lambda self: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg, slow_import=True, allow_remote_dump=False) is True
        assert remote_called == []

    def test_falls_through_to_ci(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_ci_dump", lambda self: True)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg, slow_import=True) is True

    def test_fails_when_all_strategies_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_ci_dump", lambda self: False)
        cfg = _make_cfg(tmp_path)
        assert django_db_import(cfg, slow_import=True) is False

    def test_failure_message_with_remote_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_ci_dump", lambda self: False)
        cfg = _make_cfg(tmp_path, remote_db_url="postgres://u:p@host/db")
        django_db_import(cfg, slow_import=True)
        # #1306: the hint must reference the actually-valid CLI flag (--fresh-dump),
        # not the internal `slow_import` keyword.
        assert "--fresh-dump" in capsys.readouterr().out

    def test_failure_message_without_remote_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *a, **kw: [])
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_dslr", lambda self, *, skip_dslr: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_local_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_fetch_remote_dump", lambda self: False)
        monkeypatch.setattr(DjangoDbImporter, "_try_restore_from_ci_dump", lambda self: False)
        cfg = _make_cfg(tmp_path)
        django_db_import(cfg, slow_import=True)
        assert "Configure remote_db_url" in capsys.readouterr().out

    def test_skip_dslr_passed_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured_skip: list[bool] = []
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *a, **kw: ["/bin/dslr"])

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
