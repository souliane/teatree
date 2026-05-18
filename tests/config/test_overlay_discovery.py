"""Overlay discovery from ~/.teatree.toml and installed entry points.

Split verbatim from the former monolithic ``tests/test_config.py``
(souliane/teatree#443). Covers ``discover_overlays`` (TOML config, explicit
class, tilde expansion, entry-point fallback, TOML-wins precedence),
``discover_active_overlay`` (cwd manage.py, single/multiple declared
overlays) and ``_resolve_ep_project_path``.

Integration-first per the Test-Writing Doctrine: real TOML fixtures under
``tmp_path``. Mocks are reserved for ``importlib.metadata.entry_points``
(represents installed overlay packages — otherwise we'd have to install
fixture packages per test).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.config import _resolve_ep_project_path, discover_active_overlay, discover_overlays

from ._shared import _write_manage_py, _write_toml


def test_discover_overlays_from_toml(tmp_path: Path) -> None:
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


def test_discover_overlays_with_explicit_class(tmp_path: Path) -> None:
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


def test_discover_overlays_empty_toml(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")

    result = discover_overlays(config_path=config_path)
    # No TOML-configured overlays; entry-point overlays (like t3-teatree) may
    # appear with a resolved project_path if a manage.py exists in the repo.
    toml_names = {e.name for e in result if e.project_path is not None}
    assert "my-overlay" not in toml_names  # no TOML overlay was configured


def test_discover_overlays_missing_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "nonexistent.toml"
    result = discover_overlays(config_path=config_path)
    # No TOML file at all — only entry-point overlays may appear.
    toml_names = {e.name for e in result}
    assert "my-overlay" not in toml_names


def test_discover_overlays_path_without_manage_py(tmp_path: Path) -> None:
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


def test_discover_overlays_multiple(tmp_path: Path) -> None:
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


def test_discover_overlays_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_discover_active_overlay_from_manage_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """discover_active_overlay finds overlay from manage.py in cwd ancestors."""
    _write_manage_py(tmp_path, "active.settings")
    monkeypatch.chdir(tmp_path)
    result = discover_active_overlay()
    assert result is not None
    assert result.project_path == tmp_path


def test_discover_active_overlay_single_installed(
    config_file: Path,
    elsewhere: Path,
    no_installed_overlays: None,
) -> None:
    """Single TOML-declared overlay (no manage.py in cwd ancestors) is picked as active."""
    del elsewhere, no_installed_overlays
    _write_toml(config_file, '[overlays.acme]\nclass = "acme.settings"\n')

    result = discover_active_overlay()

    assert result is not None
    assert result.name == "acme"


def test_discover_active_overlay_none_when_multiple(
    config_file: Path,
    elsewhere: Path,
    no_installed_overlays: None,
) -> None:
    """Multiple declared overlays + no cwd hint → cannot pick an active one."""
    del elsewhere, no_installed_overlays
    _write_toml(
        config_file,
        """
[overlays.a]
class = "a.settings"

[overlays.b]
class = "b.settings"
""",
    )

    assert discover_active_overlay() is None


def test_discover_active_overlay_none_when_no_overlays(
    config_file: Path,
    elsewhere: Path,
    no_installed_overlays: None,
) -> None:
    """No TOML overlays + no entry points + no manage.py → None."""
    del config_file, elsewhere, no_installed_overlays

    assert discover_active_overlay() is None


def test_discover_overlays_entry_points(tmp_path: Path) -> None:
    """Overlays can be discovered from installed entry points."""
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


def test_discover_overlays_toml_wins_over_entry_point(tmp_path: Path) -> None:
    """Toml config takes precedence over entry points with same name."""
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


def test_discover_from_manage_py_no_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_installed_overlays: None,
) -> None:
    """discover_active_overlay returns None when manage.py has no settings module and no overlays installed."""
    del no_installed_overlays
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python\npass\n")
    monkeypatch.chdir(tmp_path)

    assert discover_active_overlay() is None


# ── _resolve_ep_project_path ─────────────────────────────────────────


def test_resolve_ep_project_path_finds_manage_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolves project root by walking up from the package to manage.py."""
    project_root = tmp_path / "myproject"
    pkg_dir = project_root / "src" / "mypkg_resolve_finds"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("")
    _write_manage_py(project_root, "mypkg_resolve_finds.settings")

    monkeypatch.syspath_prepend(str(project_root / "src"))
    # Ensure a fresh import so the spec points at our tmp pkg.
    monkeypatch.delitem(sys.modules, "mypkg_resolve_finds", raising=False)

    result = _resolve_ep_project_path("mypkg_resolve_finds.settings")

    assert result == project_root


def test_resolve_ep_project_path_no_manage_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns None when no manage.py exists in any ancestor."""
    pkg_dir = tmp_path / "site-packages" / "mypkg_resolve_no_manage"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("")

    monkeypatch.syspath_prepend(str(tmp_path / "site-packages"))
    monkeypatch.delitem(sys.modules, "mypkg_resolve_no_manage", raising=False)

    assert _resolve_ep_project_path("mypkg_resolve_no_manage.settings") is None


def test_resolve_ep_project_path_unknown_package() -> None:
    """Returns None for a package that cannot be found."""
    assert _resolve_ep_project_path("nonexistent_pkg_xyz.settings") is None


def test_discover_overlays_entry_point_with_project_path(tmp_path: Path) -> None:
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
