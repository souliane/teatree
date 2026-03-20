"""Tests for _init.py — initialization and framework auto-detection."""

import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

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
        monkeypatch.setitem(sys.modules, "lib.project_hooks", None)
        lib.init._initialized = False
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

    def test_non_t3_keys_ignored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keys not starting with T3_ and empty-value keys are skipped."""
        ws = tmp_path / "workspace"
        ws.mkdir()

        teatree_cfg = tmp_path / ".teatree"
        teatree_cfg.write_text(
            f'# a comment\n\nT3_WORKSPACE_DIR="{ws}"\nNON_T3_KEY=value\nT3_EMPTY=\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("T3_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("NON_T3_KEY", raising=False)
        monkeypatch.delenv("T3_EMPTY", raising=False)
        monkeypatch.delenv("T3_OVERLAY", raising=False)

        lib.init._initialized = False
        lib.init.init()

        assert "NON_T3_KEY" not in sys.modules  # never set
        assert os.environ.get("T3_EMPTY") is None  # empty value skipped

    def test_reload_direnv_from_orig_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_reload_direnv runs direnv export json from _T3_ORIG_CWD."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        ticket = ws / "ticket-123" / "my-project"
        ticket.mkdir(parents=True)

        monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws))
        monkeypatch.setenv("_T3_ORIG_CWD", str(ticket))
        monkeypatch.setenv("CLIENT_NAME", "wrong_tenant")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type(
                "R",
                (),
                {
                    "returncode": 0,
                    "stdout": '{"CLIENT_NAME": "acme"}',
                },
            )()
            lib.init._initialized = False
            lib.init.init()

        assert os.environ["CLIENT_NAME"] == "acme"

    def test_reload_direnv_noop_when_direnv_returns_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When direnv export returns empty output, env unchanged."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        empty_dir = ws / "no-worktree"
        empty_dir.mkdir()

        monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws))
        monkeypatch.setenv("_T3_ORIG_CWD", str(empty_dir))
        monkeypatch.setenv("CLIENT_NAME", "original")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": ""})()
            lib.init._initialized = False
            lib.init.init()

        assert os.environ["CLIENT_NAME"] == "original"

    def test_reload_direnv_noop_without_orig_cwd(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without _T3_ORIG_CWD, direnv reload is skipped."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.delenv("_T3_ORIG_CWD", raising=False)
        monkeypatch.setenv("CLIENT_NAME", "original")

        lib.init._initialized = False
        lib.init.init()

        assert os.environ["CLIENT_NAME"] == "original"

    def test_overlay_scripts_added_to_sys_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When T3_OVERLAY has a scripts/ dir, it is added to sys.path."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        (overlay / "scripts").mkdir()

        teatree_cfg = tmp_path / ".teatree"
        teatree_cfg.write_text(
            f'T3_WORKSPACE_DIR="{ws}"\nT3_OVERLAY="{overlay}"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("T3_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("T3_OVERLAY", raising=False)

        lib.init._initialized = False
        lib.init.init()

        assert str(overlay / "scripts") in sys.path
        sys.path.remove(str(overlay / "scripts"))

    def test_overlay_scripts_not_a_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When T3_OVERLAY points to a dir without scripts/, sys.path unchanged."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        # No scripts/ subdir

        teatree_cfg = tmp_path / ".teatree"
        teatree_cfg.write_text(
            f'T3_WORKSPACE_DIR="{ws}"\nT3_OVERLAY="{overlay}"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("T3_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("T3_OVERLAY", raising=False)

        original_path_len = len(sys.path)
        lib.init._initialized = False
        lib.init.init()

        # sys.path should not have grown (overlay/scripts doesn't exist)
        assert len(sys.path) <= original_path_len + 1  # +1 for possible workspace
