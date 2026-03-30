"""Tests for overlay discovery from ~/.teatree.toml and entry points."""

from pathlib import Path
from unittest.mock import patch

from teatree.config import (
    _extract_settings_module,
    default_logging,
    discover_active_overlay,
    discover_overlays,
    get_data_dir,
    load_config,
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
    assert by_name["my-overlay"].settings_module == "myoverlay.settings"
    assert by_name["my-overlay"].project_path == project


def test_discover_overlays_empty_toml(tmp_path):
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")

    result = discover_overlays(config_path=config_path)
    # Only entry-point overlays (like the bundled t3-teatree) may appear
    toml_overlays = [e for e in result if e.project_path is not None]
    assert toml_overlays == []


def test_discover_overlays_missing_toml(tmp_path):
    config_path = tmp_path / "nonexistent.toml"
    result = discover_overlays(config_path=config_path)
    toml_overlays = [e for e in result if e.project_path is not None]
    assert toml_overlays == []


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
    assert by_name["empty-overlay"].settings_module == ""


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
    assert by_name["my-project"].settings_module == "myproj.settings"


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
    assert config.workspace_dir == Path("/custom/workspace")
    assert config.branch_prefix == "ac-"
    assert config.privacy == "strict"
    assert "teatree" in config.raw


def test_load_config_missing_file(tmp_path):
    config = load_config(tmp_path / "nonexistent.toml")
    assert config.workspace_dir == Path.home() / "workspace"
    assert config.branch_prefix == ""
    assert config.privacy == ""


def test_load_config_defaults_when_teatree_section_empty(tmp_path):
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[other]\nfoo = 1\n")
    config = load_config(config_path)
    assert config.branch_prefix == ""


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
    assert result.settings_module == "active.settings"
    assert result.project_path == tmp_path


def test_discover_active_overlay_single_installed(tmp_path, monkeypatch):
    """discover_active_overlay returns single installed overlay."""
    # No manage.py in cwd hierarchy
    sub = tmp_path / "no_manage"
    sub.mkdir()
    monkeypatch.chdir(sub)
    from teatree.config import OverlayEntry  # noqa: PLC0415

    single = [OverlayEntry(name="acme", settings_module="acme.settings")]
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
        OverlayEntry(name="a", settings_module="a.settings"),
        OverlayEntry(name="b", settings_module="b.settings"),
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

    with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
        result = discover_overlays(config_path=config_path)
        assert len(result) == 1
        assert result[0].name == "ep-overlay"
        assert result[0].settings_module == "ep_overlay.settings"


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
        assert result[0].settings_module == "myoverlay.settings"


def test_discover_from_manage_py_no_settings(tmp_path, monkeypatch):
    """_discover_from_manage_py returns None when manage.py has no settings."""
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python\npass\n")
    monkeypatch.chdir(tmp_path)
    result = discover_active_overlay()
    # It finds manage.py but can't extract settings module, so returns None
    with patch("teatree.config.discover_overlays", return_value=[]):
        result = discover_active_overlay()
    assert result is None
