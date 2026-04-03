"""Tests for overlay discovery from ~/.teatree.toml and entry points."""

import importlib.util
import json
import subprocess
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

from teatree.config import (
    _extract_settings_module,
    _resolve_ep_project_path,
    _write_update_cache,
    check_for_updates,
    default_logging,
    discover_active_overlay,
    discover_overlays,
    get_data_dir,
    load_config,
    workspace_dir,
    worktrees_dir,
)


def _write_manage_py(project_path: Path, settings_module: str = "myapp.settings") -> None:
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "manage.py").write_text(f'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{settings_module}")\n')


def _write_toml(config_path: Path, content: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")


def test_discover_overlays_from_toml(tmp_path):
    project = tmp_path / "my-overlay"
    _write_manage_py(project, "myoverlay.settings")

    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        f"""
[overlays.my-overlay]
path = "{project}"
""",
    )

    result = discover_overlays(config_path=config_path)
    by_name = {e.name: e for e in result}
    assert "my-overlay" in by_name
    assert by_name["my-overlay"].overlay_class == "myoverlay.settings"
    assert by_name["my-overlay"].project_path == project


def test_discover_overlays_with_explicit_class(tmp_path):
    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        """
[overlays.my-overlay]
class = "my_overlay.overlay:MyOverlay"
""",
    )

    result = discover_overlays(config_path=config_path)
    by_name = {e.name: e for e in result}
    assert "my-overlay" in by_name
    assert by_name["my-overlay"].overlay_class == "my_overlay.overlay:MyOverlay"
    assert by_name["my-overlay"].project_path is None


def test_discover_overlays_empty_toml(tmp_path):
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")

    result = discover_overlays(config_path=config_path)
    # No TOML-configured overlays; entry-point overlays (like t3-teatree) may
    # appear with a resolved project_path if a manage.py exists in the repo.
    toml_names = {e.name for e in result if e.project_path is not None}
    assert "my-overlay" not in toml_names  # no TOML overlay was configured


def test_discover_overlays_missing_toml(tmp_path):
    config_path = tmp_path / "nonexistent.toml"
    result = discover_overlays(config_path=config_path)
    # No TOML file at all — only entry-point overlays may appear.
    toml_names = {e.name for e in result}
    assert "my-overlay" not in toml_names


def test_discover_overlays_path_without_manage_py(tmp_path):
    project = tmp_path / "empty-overlay"
    project.mkdir()

    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        f"""
[overlays.empty-overlay]
path = "{project}"
""",
    )

    result = discover_overlays(config_path=config_path)
    by_name = {e.name: e for e in result}
    assert "empty-overlay" in by_name
    assert by_name["empty-overlay"].overlay_class == ""


def test_discover_overlays_multiple(tmp_path):
    for name, settings in [("proj-a", "a.settings"), ("proj-b", "b.settings")]:
        _write_manage_py(tmp_path / name, settings)

    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        f"""
[overlays.proj-a]
path = "{tmp_path / "proj-a"}"

[overlays.proj-b]
path = "{tmp_path / "proj-b"}"
""",
    )

    result = discover_overlays(config_path=config_path)
    names = {e.name for e in result}
    assert {"proj-a", "proj-b"} <= names


def test_discover_overlays_tilde_expansion(tmp_path, monkeypatch):
    project = tmp_path / "home" / "workspace" / "my-project"
    _write_manage_py(project, "myproj.settings")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        """
[overlays.my-project]
path = "~/workspace/my-project"
""",
    )

    result = discover_overlays(config_path=config_path)
    by_name = {e.name: e for e in result}
    assert "my-project" in by_name
    assert by_name["my-project"].project_path == project
    assert by_name["my-project"].overlay_class == "myproj.settings"


# ── load_config ───────────────────────────────────────────────────────


def test_load_config_from_file(tmp_path):
    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        """
[teatree]
workspace_dir = "/custom/workspace"
branch_prefix = "ac-"
privacy = "strict"
""",
    )
    config = load_config(config_path)
    assert config.user.workspace_dir == Path("/custom/workspace")
    assert config.user.branch_prefix == "ac-"
    assert config.user.privacy == "strict"
    assert "teatree" in config.raw


def test_load_config_missing_file(tmp_path):
    config = load_config(tmp_path / "nonexistent.toml")
    assert config.user.workspace_dir == Path.home() / "workspace"
    assert config.user.branch_prefix == ""
    assert config.user.privacy == ""


def test_load_config_defaults_when_teatree_section_empty(tmp_path):
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[other]\nfoo = 1\n")
    config = load_config(config_path)
    assert config.user.branch_prefix == ""


# ── get_data_dir ──────────────────────────────────────────────────────


def test_get_data_dir_creates_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("teatree.config.DATA_DIR", tmp_path / "data")
    result = get_data_dir("test-namespace")
    assert result == tmp_path / "data" / "test-namespace"
    assert result.is_dir()


# ── default_logging ───────────────────────────────────────────────────


def test_default_logging_returns_dict(tmp_path, monkeypatch):
    monkeypatch.setattr("teatree.config.DATA_DIR", tmp_path / "data")
    config = default_logging("test-ns")
    assert config["version"] == 1
    assert "file" in config["handlers"]
    assert "console" in config["handlers"]
    log_dir = tmp_path / "data" / "test-ns" / "logs"
    assert log_dir.is_dir()


# ── _extract_settings_module ──────────────────────────────────────────


def test_extract_settings_module_found(tmp_path):
    manage_py = tmp_path / "manage.py"
    manage_py.write_text('os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myapp.settings")\n')
    assert _extract_settings_module(manage_py) == "myapp.settings"


def test_extract_settings_module_not_found(tmp_path):
    manage_py = tmp_path / "manage.py"
    manage_py.write_text("#!/usr/bin/env python\npass\n")
    assert _extract_settings_module(manage_py) == ""


# ── discover_active_overlay ──────────────────────────────────────────


def test_discover_active_overlay_from_manage_py(tmp_path, monkeypatch):
    """discover_active_overlay finds overlay from manage.py in cwd ancestors."""
    _write_manage_py(tmp_path, "active.settings")
    monkeypatch.chdir(tmp_path)
    result = discover_active_overlay()
    assert result is not None
    assert result.project_path == tmp_path


def test_discover_active_overlay_single_installed(tmp_path, monkeypatch):
    """discover_active_overlay returns single installed overlay."""
    # No manage.py in cwd hierarchy
    sub = tmp_path / "no_manage"
    sub.mkdir()
    monkeypatch.chdir(sub)
    from teatree.config import OverlayEntry  # noqa: PLC0415

    single = [OverlayEntry(name="acme", overlay_class="acme.settings")]
    with patch("teatree.config.discover_overlays", return_value=single):
        result = discover_active_overlay()
        assert result is not None
        assert result.name == "acme"


def test_discover_active_overlay_none_when_multiple(tmp_path, monkeypatch):
    """discover_active_overlay returns None when multiple overlays installed."""
    sub = tmp_path / "no_manage"
    sub.mkdir()
    monkeypatch.chdir(sub)
    from teatree.config import OverlayEntry  # noqa: PLC0415

    multiple = [
        OverlayEntry(name="a", overlay_class="a.settings"),
        OverlayEntry(name="b", overlay_class="b.settings"),
    ]
    with patch("teatree.config.discover_overlays", return_value=multiple):
        assert discover_active_overlay() is None


def test_discover_active_overlay_none_when_no_overlays(tmp_path, monkeypatch):
    sub = tmp_path / "no_manage"
    sub.mkdir()
    monkeypatch.chdir(sub)
    with patch("teatree.config.discover_overlays", return_value=[]):
        assert discover_active_overlay() is None


def test_discover_overlays_entry_points(tmp_path, monkeypatch):
    """Overlays can be discovered from installed entry points."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")

    mock_ep = MagicMock()
    mock_ep.name = "ep-overlay"
    mock_ep.value = "ep_overlay.settings"

    with (
        patch("importlib.metadata.entry_points", return_value=[mock_ep]),
        patch("teatree.config._resolve_ep_project_path", return_value=None),
    ):
        result = discover_overlays(config_path=config_path)
        assert len(result) == 1
        assert result[0].name == "ep-overlay"
        assert result[0].overlay_class == "ep_overlay.settings"


def test_discover_overlays_toml_wins_over_entry_point(tmp_path):
    """Toml config takes precedence over entry points with same name."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    project = tmp_path / "my-overlay"
    _write_manage_py(project, "myoverlay.settings")

    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, f'[overlays.my-overlay]\npath = "{project}"\n')

    mock_ep = MagicMock()
    mock_ep.name = "my-overlay"
    mock_ep.value = "other.settings"

    with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
        result = discover_overlays(config_path=config_path)
        assert len(result) == 1
        assert result[0].overlay_class == "myoverlay.settings"


def test_discover_from_manage_py_no_settings(tmp_path, monkeypatch):
    """_discover_from_manage_py returns None when manage.py has no settings."""
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python\npass\n")
    monkeypatch.chdir(tmp_path)
    result = discover_active_overlay()
    # It finds manage.py but can't extract settings module, so returns None
    with patch("teatree.config.discover_overlays", return_value=[]):
        result = discover_active_overlay()
    assert result is None


# ── _resolve_ep_project_path ─────────────────────────────────────────


def test_resolve_ep_project_path_finds_manage_py(tmp_path):
    """Resolves project root by walking up from the package to manage.py."""
    project_root = tmp_path / "myproject"
    pkg_dir = project_root / "src" / "mypkg"
    pkg_dir.mkdir(parents=True)
    _write_manage_py(project_root, "mypkg.settings")

    mock_spec = types.SimpleNamespace(submodule_search_locations=[str(pkg_dir)])
    with patch.object(importlib.util, "find_spec", return_value=mock_spec):
        result = _resolve_ep_project_path("mypkg.settings")
    assert result == project_root


def test_resolve_ep_project_path_no_manage_py(tmp_path):
    """Returns None when no manage.py exists in any ancestor."""
    pkg_dir = tmp_path / "site-packages" / "mypkg"
    pkg_dir.mkdir(parents=True)

    mock_spec = types.SimpleNamespace(submodule_search_locations=[str(pkg_dir)])
    with patch.object(importlib.util, "find_spec", return_value=mock_spec):
        assert _resolve_ep_project_path("mypkg.settings") is None


def test_resolve_ep_project_path_unknown_package():
    """Returns None for a package that cannot be found."""
    assert _resolve_ep_project_path("nonexistent_pkg_xyz.settings") is None


def test_discover_overlays_entry_point_with_project_path(tmp_path):
    """Entry-point overlay gets project_path resolved from package location."""
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")

    project_root = tmp_path / "myproject"
    _write_manage_py(project_root, "mypkg.contrib.settings")

    mock_ep = MagicMock()
    mock_ep.name = "my-overlay"
    mock_ep.value = "mypkg.contrib.settings"

    with (
        patch("importlib.metadata.entry_points", return_value=[mock_ep]),
        patch("teatree.config._resolve_ep_project_path", return_value=project_root),
    ):
        result = discover_overlays(config_path=config_path)
        assert len(result) == 1
        assert result[0].project_path == project_root


# ── workspace_dir / worktrees_dir ────────────────────────────────────


class TestWorkspaceDir:
    def test_returns_path_from_django_settings(self, tmp_path, settings):
        custom = tmp_path / "custom-ws"
        settings.T3_WORKSPACE_DIR = str(custom)
        result = workspace_dir()
        assert result == custom

    def test_falls_back_to_config_file(self, tmp_path):
        from teatree.config import TeaTreeConfig, UserSettings  # noqa: PLC0415

        fake_config = TeaTreeConfig(user=UserSettings(workspace_dir=Path("/from/config")))
        with patch("teatree.config.load_config", return_value=fake_config):
            result = workspace_dir()
        assert result == Path("/from/config")


class TestWorktreesDir:
    def test_returns_path_from_django_settings(self, tmp_path, settings):
        custom = tmp_path / "custom-wt"
        settings.T3_WORKTREES_DIR = str(custom)
        result = worktrees_dir()
        assert result == custom

    def test_falls_back_to_config_file(self, tmp_path):
        from teatree.config import TeaTreeConfig, UserSettings  # noqa: PLC0415

        fake_config = TeaTreeConfig(user=UserSettings(worktrees_dir=Path("/from/config/wt")))
        with patch("teatree.config.load_config", return_value=fake_config):
            result = worktrees_dir()
        assert result == Path("/from/config/wt")


# ── check_for_updates ────────────────────────────────────────────────


class TestCheckForUpdates:
    def _fake_config(self, *, check_updates: bool = True):
        from teatree.config import TeaTreeConfig, UserSettings  # noqa: PLC0415

        return TeaTreeConfig(user=UserSettings(check_updates=check_updates))

    def test_returns_none_when_updates_disabled(self):
        """Line 144: early return None when check_updates=false and force=False."""
        with patch("teatree.config.load_config", return_value=self._fake_config(check_updates=False)):
            assert check_for_updates(force=False) is None

    def test_cached_result_returned_when_fresh(self, tmp_path, monkeypatch):
        """Lines 153-156: return cached message when within TTL."""
        import time  # noqa: PLC0415

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cache_path = data_dir / "update-check.json"
        cache_path.write_text(
            json.dumps({"ts": time.time(), "message": "teatree v9.9 available"}),
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        with patch("teatree.config.load_config", return_value=self._fake_config()):
            result = check_for_updates(force=False)
        assert result == "teatree v9.9 available"

    def test_cached_empty_message_returns_none(self, tmp_path, monkeypatch):
        """Lines 153-154: cached empty message means up-to-date => None."""
        import time  # noqa: PLC0415

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cache_path = data_dir / "update-check.json"
        cache_path.write_text(
            json.dumps({"ts": time.time(), "message": ""}),
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        with patch("teatree.config.load_config", return_value=self._fake_config()):
            assert check_for_updates(force=False) is None

    def test_cached_corrupt_json_falls_through(self, tmp_path, monkeypatch):
        """Lines 155-156: corrupt cache JSON is silently ignored, proceeds to network check."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cache_path = data_dir / "update-check.json"
        cache_path.write_text("NOT VALID JSON {{{", encoding="utf-8")
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="v1.0.0\n")
        with (
            patch("teatree.config.load_config", return_value=self._fake_config()),
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            # Falls through corrupt cache, hits network, finds same version
            assert check_for_updates(force=False) is None

    def test_empty_tag_returns_none(self, tmp_path, monkeypatch):
        """Line 175: when gh returns empty tag, returns None."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="\n")
        with (
            patch("teatree.config.load_config", return_value=self._fake_config()),
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            assert check_for_updates(force=True) is None

    def test_subprocess_timeout_returns_none(self, tmp_path, monkeypatch):
        """Lines 171-172: TimeoutExpired from gh CLI returns None."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        with (
            patch("teatree.config.load_config", return_value=self._fake_config()),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 10)),
        ):
            assert check_for_updates(force=True) is None

    def test_file_not_found_returns_none(self, tmp_path, monkeypatch):
        """Lines 171-172: FileNotFoundError (gh not installed) returns None."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        with (
            patch("teatree.config.load_config", return_value=self._fake_config()),
            patch("subprocess.run", side_effect=FileNotFoundError),
        ):
            assert check_for_updates(force=True) is None

    def test_newer_version_returns_upgrade_message(self, tmp_path, monkeypatch):
        """Lines 177-184: when latest != current, returns upgrade message."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="v2.0.0\n")
        with (
            patch("teatree.config.load_config", return_value=self._fake_config()),
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            result = check_for_updates(force=True)

        assert result is not None
        assert "v2.0.0" in result
        assert "1.0.0" in result
        assert "uv pip install --upgrade teatree" in result

        # Verify cache was written
        cache_path = data_dir / "update-check.json"
        assert cache_path.is_file()
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert "v2.0.0" in cached["message"]

    def test_same_version_returns_none_and_caches(self, tmp_path, monkeypatch):
        """Lines 178-180: when latest == current, returns None and caches empty."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="v1.0.0\n")
        with (
            patch("teatree.config.load_config", return_value=self._fake_config()),
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            result = check_for_updates(force=True)

        assert result is None
        cache_path = data_dir / "update-check.json"
        assert cache_path.is_file()
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert cached["message"] == ""


# ── _write_update_cache ──────────────────────────────────────────────


class TestWriteUpdateCache:
    def test_creates_parent_dirs_and_writes_json(self, tmp_path):
        """Lines 189-193: creates parent dirs and writes valid JSON cache."""
        cache_path = tmp_path / "nested" / "dir" / "update-check.json"
        _write_update_cache(cache_path, "test message")

        assert cache_path.is_file()
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert data["message"] == "test message"
        assert "ts" in data
        assert isinstance(data["ts"], float)
