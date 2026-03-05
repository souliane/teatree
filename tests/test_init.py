"""Tests for _init.py — initialization and framework auto-detection."""

import sys
import types
from pathlib import Path

import lib.init
import pytest
from lib.registry import get, registered_points


class TestInit:
    def test_registers_default_extension_points(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        lib.init._initialized = False
        lib.init.init()

        assert "wt_symlinks" in registered_points()
        assert "wt_post_db" in registered_points()

    def test_idempotent(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        lib.init._initialized = False
        lib.init.init()
        lib.init.init()  # Should not raise or re-register

    def test_detects_django_framework(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        # workspace fixture already has my-project/manage.py
        lib.init._initialized = False
        lib.init.init()

        # Django plugin registers wt_run_tests at framework layer
        handler = get("wt_run_tests")
        assert handler is not None
        assert handler.__module__ == "frameworks.django"

    def test_no_framework_without_manage_py(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "some-repo").mkdir()
        (ws / "some-repo" / ".git").mkdir()

        monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws))
        lib.init._initialized = False
        lib.init.init()

        # wt_run_tests should be the default no-op
        handler = get("wt_run_tests")
        assert handler is not None
        assert handler.__module__ == "lib.extension_points"

    def test_handles_oserror_scanning_workspace(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OSError during os.scandir (e.g. permission denied) is handled."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", "/nonexistent/workspace")
        lib.init._initialized = False
        lib.init.init()

        # Should still register defaults even if framework detection fails
        assert "wt_symlinks" in registered_points()

    def test_project_hooks_not_found_is_silent(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When lib.project_hooks is not importable, init() succeeds silently."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        lib.init._initialized = False
        # lib.project_hooks is not on PYTHONPATH in the test env,
        # so this exercises the except ImportError branch.
        lib.init.init()
        assert "wt_symlinks" in registered_points()

    def test_project_hooks_called_when_available(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When lib.project_hooks is importable, register() is called."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))

        called = []
        fake_module = types.ModuleType("lib.project_hooks")
        fake_module.register = lambda: called.append(True)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "lib.project_hooks", fake_module)

        lib.init._initialized = False
        lib.init.init()

        assert called
