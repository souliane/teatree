"""Tests for :mod:`teatree.self_update` — the shared reinstall + self-DB migrate.

Mirrors ``src/teatree/self_update.py``. The only externals stubbed are the
host-machine ``uv`` / ``t3`` / ``python -m teatree migrate`` shell-outs (the
runner is injected, or :func:`teatree.utils.run.run_allowed_to_fail` is
patched at the module level); the editable-source receipt parse is exercised
against a real ``uv-receipt.toml`` on disk.
"""

from dataclasses import dataclass
from pathlib import Path

import click
import pytest

import teatree.self_update as self_update_mod
from teatree.self_update import (
    ReinstallResult,
    _migrate_self_db,
    _self_db_has_pending_migrations,
    current_editable_source,
    ensure_self_db_migrated,
    reinstall_running_editable,
    seed_db_config_from_toml,
)


@dataclass
class _Proc:
    """Stand-in CompletedProcess for the host-machine ``uv`` / ``t3`` shell-outs."""

    returncode: int
    stdout: str
    stderr: str


def _which_all(name: str) -> str:
    return f"/usr/bin/{name}"


class TestCurrentEditableSource:
    """Parse the editable source from uv's tool receipt against a real file."""

    def _receipt(self, tmp_path: Path, body: str) -> Path:
        tool_dir = tmp_path / "uv-tools"
        (tool_dir / "teatree").mkdir(parents=True)
        (tool_dir / "teatree" / "uv-receipt.toml").write_text(body, encoding="utf-8")
        return tool_dir

    def test_returns_editable_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        src = tmp_path / "clone"
        tool_dir = self._receipt(
            tmp_path,
            f'[tool]\nrequirements = [{{ name = "teatree", editable = "{src}" }}]\n',
        )
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, str(tool_dir), ""))

        assert current_editable_source("/usr/bin/uv") == src

    def test_returns_none_when_uv_dir_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(1, "", "no tools"))

        assert current_editable_source("/usr/bin/uv") is None

    def test_returns_none_when_uv_dir_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, "   ", ""))

        assert current_editable_source("/usr/bin/uv") is None

    def test_returns_none_when_receipt_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "teatree").mkdir()
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, str(tmp_path), ""))

        assert current_editable_source("/usr/bin/uv") is None

    def test_returns_none_when_receipt_unparsable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tool_dir = self._receipt(tmp_path, "this is = not [ valid toml")
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, str(tool_dir), ""))

        assert current_editable_source("/usr/bin/uv") is None

    def test_returns_none_for_non_editable_install(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tool_dir = self._receipt(tmp_path, '[tool]\nrequirements = [{ name = "teatree" }]\n')
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, str(tool_dir), ""))

        assert current_editable_source("/usr/bin/uv") is None

    def test_returns_none_when_teatree_not_in_requirements(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool_dir = self._receipt(tmp_path, '[tool]\nrequirements = [{ name = "other", editable = "/x" }]\n')
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, str(tool_dir), ""))

        assert current_editable_source("/usr/bin/uv") is None


class TestReinstallRunningEditable:
    """The shared ``uv tool install --editable --reinstall`` + ``t3 setup``."""

    def test_reinstalls_and_runs_setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        source = tmp_path / "editable-src"
        source.mkdir()
        calls: list[list[str]] = []

        def _runner(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "ok", "")

        monkeypatch.setattr(self_update_mod.shutil, "which", _which_all)
        monkeypatch.setattr(self_update_mod, "current_editable_source", lambda _uv: source)

        result = reinstall_running_editable(runner=_runner)

        assert result.ok is True
        assert result.reinstalled is True
        assert any("tool" in c and "install" in c and "--reinstall" in c for c in calls)
        assert any(c[-1] == "setup" for c in calls)

    def test_skips_reinstall_for_non_editable_install_but_runs_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _runner(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "setup ran", "")

        monkeypatch.setattr(self_update_mod.shutil, "which", _which_all)
        monkeypatch.setattr(self_update_mod, "current_editable_source", lambda _uv: None)

        result = reinstall_running_editable(runner=_runner)

        assert result.ok is True
        assert result.reinstalled is False
        assert not any("install" in c for c in calls)
        assert calls[-1][-1] == "setup"

    def test_no_uv_on_path_only_runs_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _runner(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "", "")

        monkeypatch.setattr(self_update_mod.shutil, "which", lambda _name: None)

        result = reinstall_running_editable(runner=_runner)

        assert result.reinstalled is False
        assert len(calls) == 1
        assert calls[0][-1] == "setup"

    def test_reports_error_when_reinstall_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        source = tmp_path / "editable-src"
        source.mkdir()

        def _runner(cmd: list[str], **_kw: object) -> _Proc:
            if "install" in cmd:
                return _Proc(1, "", "boom")
            return _Proc(0, "ok", "")

        monkeypatch.setattr(self_update_mod.shutil, "which", _which_all)
        monkeypatch.setattr(self_update_mod, "current_editable_source", lambda _uv: source)

        result = reinstall_running_editable(runner=_runner)

        assert result.ok is False
        assert "reinstall: boom" in result.error

    def test_reports_error_when_setup_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _runner(cmd: list[str], **_kw: object) -> _Proc:
            return _Proc(1, "", "setup blew up")

        monkeypatch.setattr(self_update_mod.shutil, "which", lambda _name: None)

        result = reinstall_running_editable(runner=_runner)

        assert result.ok is False
        assert "setup: setup blew up" in result.error

    def test_ignores_recorded_source_that_no_longer_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A receipt pointing at a deleted clone must not attempt a reinstall
        # from a missing dir — only `t3 setup` runs.
        gone = tmp_path / "deleted-clone"
        calls: list[list[str]] = []

        def _runner(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "", "")

        monkeypatch.setattr(self_update_mod.shutil, "which", _which_all)
        monkeypatch.setattr(self_update_mod, "current_editable_source", lambda _uv: gone)

        result = reinstall_running_editable(runner=_runner)

        assert result.reinstalled is False
        assert not any("install" in c for c in calls)


class TestSelfDbMigrate:
    """Probe + migrate the runtime self-DB in-process (#126, #929, #870)."""

    def test_migrate_runs_in_runtime_interpreter_not_uv_directory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "No migrations to apply.", "")

        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", _run)

        _migrate_self_db()

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == self_update_mod.sys.executable, "must use the running interpreter"
        assert cmd[1:4] == ["-m", "teatree", "migrate"], f"must be `python -m teatree migrate`, got {cmd!r}"
        assert "--no-input" in cmd
        assert "--directory" not in cmd, "must NOT route through `uv --directory <clone>`"

    def test_migrate_does_not_inherit_caller_settings_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "worktree_only.settings_local")
        captured: list[dict[str, str]] = []

        def _run(cmd: list[str], *, env: dict[str, str] | None = None, **_kw: object) -> _Proc:
            captured.append(dict(env or {}))
            return _Proc(0, "", "")

        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", _run)

        _migrate_self_db()

        assert captured, "migrate subprocess was not invoked"
        assert captured[0].get("DJANGO_SETTINGS_MODULE") == "teatree.settings"

    def test_migrate_fails_closed_on_failure(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(1, "", "locked"))

        with pytest.raises((SystemExit, click.exceptions.Exit)):
            _migrate_self_db()

        assert "self-DB migration" in capsys.readouterr().out

    def test_probe_reports_pending_migrations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(1, "", "")  # Django `migrate --check` exits 1 when pending

        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", _run)

        assert _self_db_has_pending_migrations() is True
        assert calls[0][-3:] == ["migrate", "--check", "--no-input"]

    def test_probe_reports_clean_when_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, "", ""))

        assert _self_db_has_pending_migrations() is False

    def test_ensure_migrates_when_pending(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            if "--check" in cmd:
                return _Proc(1, "", "")  # pending
            return _Proc(0, "Applying ...", "")

        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", _run)

        failed = ensure_self_db_migrated()

        assert failed is False
        migrate_calls = [c for c in calls if c[-2:] == ["migrate", "--no-input"]]
        assert len(migrate_calls) == 1
        assert "migrations applied" in capsys.readouterr().out

    def test_ensure_skips_migrate_when_clean(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "", "")  # probe: nothing pending

        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", _run)

        failed = ensure_self_db_migrated()

        assert failed is False
        assert not [c for c in calls if c[-2:] == ["migrate", "--no-input"]]
        assert "already migrated" in capsys.readouterr().out

    def test_ensure_quiet_emits_nothing_when_clean(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, "", ""))

        failed = ensure_self_db_migrated(quiet=True)

        assert failed is False
        assert capsys.readouterr().out == ""

    def test_ensure_quiet_still_reports_when_migrating(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _run(cmd: list[str], **_kw: object) -> _Proc:
            if "--check" in cmd:
                return _Proc(1, "", "")  # pending
            return _Proc(0, "Applying ...", "")

        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", _run)

        failed = ensure_self_db_migrated(quiet=True)

        assert failed is False
        assert "migrations applied" in capsys.readouterr().out

    def test_ensure_returns_failed_when_migration_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _run(cmd: list[str], **_kw: object) -> _Proc:
            if "--check" in cmd:
                return _Proc(1, "", "")  # pending
            return _Proc(1, "", "db locked")  # migrate fails

        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", _run)

        failed = ensure_self_db_migrated()

        assert failed is True
        assert "self-DB" in capsys.readouterr().out


class TestSeedDbConfigFromToml:
    """The ``t3 setup`` auto-migration (#938, TODO-75): seed the DB store from TOML.

    Runs ``python -m teatree config_setting import --no-clobber`` in the runtime
    interpreter, mirroring the self-DB migrate shape. Best-effort: a failure is a
    WARN, never fail-closed — the TOML stays readable and the dual-read resolver
    falls through, unlike the self-DB migrate the merge gate depends on.
    """

    def test_runs_no_clobber_import_in_runtime_interpreter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "  imported 0 setting(s) into the DB store", "")

        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", _run)

        seed_db_config_from_toml()

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == self_update_mod.sys.executable, "must use the running interpreter"
        assert cmd[1:4] == ["-m", "teatree", "config_setting"], f"got {cmd!r}"
        assert "import" in cmd
        assert "--no-clobber" in cmd, "the setup auto-migration must never clobber a DB-set value"
        assert "--directory" not in cmd, "must NOT route through `uv --directory <clone>`"

    def test_does_not_inherit_caller_settings_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "worktree_only.settings_local")
        captured: list[dict[str, str]] = []

        def _run(cmd: list[str], *, env: dict[str, str] | None = None, **_kw: object) -> _Proc:
            captured.append(dict(env or {}))
            return _Proc(0, "", "")

        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", _run)

        seed_db_config_from_toml()

        assert captured, "import subprocess was not invoked"
        assert captured[0].get("DJANGO_SETTINGS_MODULE") == "teatree.settings"

    def test_failure_is_a_warn_not_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(self_update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(1, "", "boom"))

        # Best-effort: returns without raising, unlike the fail-closed self-DB migrate.
        seed_db_config_from_toml()

        assert "config" in capsys.readouterr().out.lower()


class TestReinstallResult:
    def test_default_error_is_empty(self) -> None:
        assert ReinstallResult(ok=True, reinstalled=True).error == ""
