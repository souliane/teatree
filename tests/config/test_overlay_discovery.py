"""Overlay discovery from the DB ``overlays`` registry and installed entry points.

Covers ``discover_overlays`` (DB registry path/class, tilde expansion, entry-point
fallback, registry-wins precedence), ``discover_active_overlay`` (cwd manage.py,
single/multiple declared overlays) and ``_resolve_ep_project_path``.

Integration-first per the Test-Writing Doctrine: the ``overlays`` registry is
seeded into a real cold-path sqlite (``config_db`` + ``_seed_config_db``) with a
real on-disk ``manage.py`` under ``tmp_path``. ``importlib.metadata.entry_points``
is mocked (represents installed overlay packages).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.config import OverlayEntry, _resolve_ep_project_path, discover_active_overlay, discover_overlays

from ._shared import _seed_config_db, _write_manage_py


@pytest.mark.usefixtures("no_installed_overlays")
def test_discover_overlays_from_db_path(tmp_path: Path, config_db: Path) -> None:
    project = tmp_path / "my-overlay"
    _write_manage_py(project, "myoverlay.settings")
    _seed_config_db(config_db, overlays={"my-overlay": {"path": str(project)}})

    by_name = {e.name: e for e in discover_overlays()}
    assert by_name["my-overlay"].overlay_class == "myoverlay.settings"
    assert by_name["my-overlay"].project_path == project


@pytest.mark.usefixtures("no_installed_overlays")
def test_discover_overlays_with_explicit_class(config_db: Path) -> None:
    _seed_config_db(config_db, overlays={"my-overlay": {"class": "my_overlay.overlay:MyOverlay"}})

    by_name = {e.name: e for e in discover_overlays()}
    assert by_name["my-overlay"].overlay_class == "my_overlay.overlay:MyOverlay"
    assert by_name["my-overlay"].project_path is None


@pytest.mark.usefixtures("no_installed_overlays")
def test_discover_overlays_empty_registry(config_db: Path) -> None:
    del config_db  # no rows seeded and no entry points -> nothing discovered
    assert discover_overlays() == []


@pytest.mark.usefixtures("no_installed_overlays")
def test_discover_overlays_path_without_manage_py(tmp_path: Path, config_db: Path) -> None:
    project = tmp_path / "empty-overlay"
    project.mkdir()
    _seed_config_db(config_db, overlays={"empty-overlay": {"path": str(project)}})

    by_name = {e.name: e for e in discover_overlays()}
    assert by_name["empty-overlay"].overlay_class == ""


@pytest.mark.usefixtures("no_installed_overlays")
def test_discover_overlays_multiple(tmp_path: Path, config_db: Path) -> None:
    for name, settings in [("proj-a", "a.settings"), ("proj-b", "b.settings")]:
        _write_manage_py(tmp_path / name, settings)
    _seed_config_db(
        config_db,
        overlays={
            "proj-a": {"path": str(tmp_path / "proj-a")},
            "proj-b": {"path": str(tmp_path / "proj-b")},
        },
    )

    names = {e.name for e in discover_overlays()}
    assert names == {"proj-a", "proj-b"}


@pytest.mark.usefixtures("no_installed_overlays")
def test_discover_overlays_tilde_expansion(tmp_path: Path, config_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "home" / "workspace" / "my-project"
    _write_manage_py(project, "myproj.settings")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _seed_config_db(config_db, overlays={"my-project": {"path": "~/workspace/my-project"}})

    by_name = {e.name: e for e in discover_overlays()}
    assert by_name["my-project"].project_path == project
    assert by_name["my-project"].overlay_class == "myproj.settings"


def test_unregistered_manage_py_does_not_select_an_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_manage_py(tmp_path, "active.settings")
    monkeypatch.chdir(tmp_path)
    entries = [MagicMock(name="first"), MagicMock(name="second")]
    entries[0].name = "t3-teatree"
    entries[0].value = "teatree.contrib.t3_teatree.overlay:TeatreeOverlay"
    entries[1].name = "t3-acme"
    entries[1].value = "acme_overlay.overlay:AcmeOverlay"
    with patch("importlib.metadata.entry_points", return_value=entries):
        assert discover_active_overlay() is None


def test_discover_active_overlay_canonicalizes_clone_dir_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkout with one installed overlay resolves to that registered name."""
    project = tmp_path / "teatree"
    _write_manage_py(project, "active.settings")
    monkeypatch.chdir(project)

    ep = MagicMock()
    ep.name = "t3-teatree"
    ep.value = "t3_teatree.overlay:TeatreeOverlay"

    with patch("importlib.metadata.entry_points", return_value=[ep]):
        result = discover_active_overlay()

    assert result is not None
    assert result.name == "t3-teatree"
    assert result.project_path == project


def test_discover_active_overlay_folds_deploy_dirname_to_sole_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout with an arbitrary directory name uses its sole installed overlay."""
    project = tmp_path / "teatree-deploy"
    _write_manage_py(project, "active.settings")
    monkeypatch.chdir(project)

    ep = MagicMock()
    ep.name = "t3-teatree"
    ep.value = "t3_teatree.overlay:TeatreeOverlay"

    with patch("importlib.metadata.entry_points", return_value=[ep]):
        result = discover_active_overlay()

    assert result is not None
    assert result.name == "t3-teatree"
    assert result.project_path == project


def test_discover_active_overlay_uses_entry_point_project_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkout selects its owning installed overlay by project path."""
    project = tmp_path / "acme-factory"
    _write_manage_py(project, "acme.settings")
    monkeypatch.chdir(project)

    ep_acme = MagicMock()
    ep_acme.name = "t3-acme"
    ep_acme.value = "acme_overlay.overlay:AcmeOverlay"
    ep_teatree = MagicMock()
    ep_teatree.name = "t3-teatree"
    ep_teatree.value = "t3_teatree.overlay:TeatreeOverlay"

    with (
        patch("importlib.metadata.entry_points", return_value=[ep_acme, ep_teatree]),
        patch(
            "teatree.config.discovery._resolve_ep_project_path",
            side_effect=lambda value: project if value == ep_acme.value else None,
        ),
    ):
        result = discover_active_overlay()

    assert result is not None
    assert result.name == "t3-acme"
    assert result.project_path == project


def test_discover_active_overlay_ambiguous_dirname_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With two installed overlays an ambiguous basename yields no active overlay."""
    project = tmp_path / "teatree-deploy"
    _write_manage_py(project, "active.settings")
    monkeypatch.chdir(project)

    ep_a = MagicMock()
    ep_a.name = "t3-teatree"
    ep_a.value = "t3_teatree.overlay:TeatreeOverlay"
    ep_b = MagicMock()
    ep_b.name = "t3-acme"
    ep_b.value = "acme_pkg.overlay:AcmeOverlay"

    with patch("importlib.metadata.entry_points", return_value=[ep_a, ep_b]):
        result = discover_active_overlay()

    assert result is None


def test_discover_active_overlay_single_declared(
    config_db: Path,
    elsewhere: Path,
    no_installed_overlays: None,
) -> None:
    """A single registry-declared overlay (no manage.py in cwd ancestors) is picked as active."""
    del elsewhere, no_installed_overlays
    _seed_config_db(config_db, overlays={"acme": {"class": "acme.settings"}})

    result = discover_active_overlay()

    assert result is not None
    assert result.name == "acme"


def test_discover_active_overlay_none_when_multiple(
    config_db: Path,
    elsewhere: Path,
    no_installed_overlays: None,
) -> None:
    """Multiple declared overlays + no cwd hint -> cannot pick an active one."""
    del elsewhere, no_installed_overlays
    _seed_config_db(config_db, overlays={"a": {"class": "a.settings"}, "b": {"class": "b.settings"}})

    assert discover_active_overlay() is None


def test_discover_active_overlay_none_when_no_overlays(
    config_db: Path,
    elsewhere: Path,
    no_installed_overlays: None,
) -> None:
    """No declared overlays + no entry points + no manage.py -> None."""
    del config_db, elsewhere, no_installed_overlays

    assert discover_active_overlay() is None


def test_discover_overlays_entry_points(config_db: Path) -> None:
    """Overlays can be discovered from installed entry points."""
    del config_db

    mock_ep = MagicMock()
    mock_ep.name = "ep-overlay"
    mock_ep.value = "ep_overlay.settings"

    with (
        patch("importlib.metadata.entry_points", return_value=[mock_ep]),
        patch("teatree.config.discovery._resolve_ep_project_path", return_value=None),
    ):
        result = discover_overlays()
        assert len(result) == 1
        assert result[0].name == "ep-overlay"
        assert result[0].overlay_class == "ep_overlay.settings"


def test_db_registry_wins_over_entry_point(tmp_path: Path, config_db: Path) -> None:
    """The DB registry takes precedence over an entry point with the same name."""
    project = tmp_path / "my-overlay"
    _write_manage_py(project, "myoverlay.settings")
    _seed_config_db(config_db, overlays={"my-overlay": {"path": str(project)}})

    mock_ep = MagicMock()
    mock_ep.name = "my-overlay"
    mock_ep.value = "other.settings"

    with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
        result = discover_overlays()
        assert len(result) == 1
        assert result[0].overlay_class == "myoverlay.settings"


# ── registry paths across a container boundary ───────────────────────


def _overlay_entry_point(name: str, value: str) -> MagicMock:
    ep = MagicMock()
    ep.name = name
    ep.value = value
    return ep


def test_registry_path_from_the_other_side_resolves_through_the_installed_package(
    tmp_path: Path,
    config_db: Path,
) -> None:
    """A stored ``path`` that names nothing here falls back to where the package really is.

    The DB-home ``overlays`` registry is shared by host and container, so a
    ``~``-rooted path written on the host names nothing inside the container (the
    fork is bind-mounted elsewhere). Left unresolved it degrades every consumer:
    ``t3 info`` prints a dead path and ``overlay_class`` stays blank because no
    ``manage.py`` is found to read the settings module out of.
    """
    fork_root = tmp_path / "container" / "teatree"
    _write_manage_py(fork_root, "fork.settings")
    _seed_config_db(config_db, overlays={"t3-fork": {"path": "/home/other-side/workspace/org/fork"}})

    with (
        patch("importlib.metadata.entry_points", return_value=[_overlay_entry_point("t3-fork", "fork_overlay:Fork")]),
        patch("teatree.config.discovery._resolve_ep_project_path", return_value=fork_root),
    ):
        by_name = {e.name: e for e in discover_overlays()}

    assert by_name["t3-fork"].project_path == fork_root
    assert by_name["t3-fork"].overlay_class == "fork.settings"


def test_registry_path_that_resolves_is_never_replaced(tmp_path: Path, config_db: Path) -> None:
    """Host and the deployment box (paths coincide) keep the configured path verbatim."""
    project = tmp_path / "my-overlay"
    _write_manage_py(project, "myoverlay.settings")
    _seed_config_db(config_db, overlays={"my-overlay": {"path": str(project)}})

    with (
        patch(
            "importlib.metadata.entry_points",
            return_value=[_overlay_entry_point("my-overlay", "my_overlay:MyOverlay")],
        ),
        patch("teatree.config.discovery._resolve_ep_project_path", return_value=tmp_path / "decoy"),
    ):
        by_name = {e.name: e for e in discover_overlays()}

    assert by_name["my-overlay"].project_path == project


@pytest.mark.usefixtures("no_installed_overlays")
def test_unresolvable_registry_path_is_reported_as_configured(tmp_path: Path, config_db: Path) -> None:
    """With nothing installed to resolve against, the operator's own path is preserved."""
    del tmp_path
    _seed_config_db(config_db, overlays={"my-overlay": {"path": "/nowhere/my-overlay"}})

    by_name = {e.name: e for e in discover_overlays()}

    assert by_name["my-overlay"].project_path == Path("/nowhere/my-overlay")


@pytest.mark.usefixtures("no_installed_overlays")
def test_malformed_class_does_not_take_discovery_down(config_db: Path) -> None:
    """Discovery runs on nearly every CLI path — bad operator config must not lock it out."""
    _seed_config_db(config_db, overlays={"my-overlay": {"path": "/nowhere/my-overlay", "class": ":Broken"}})

    by_name = {e.name: e for e in discover_overlays()}

    assert by_name["my-overlay"].project_path == Path("/nowhere/my-overlay")


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


def test_discover_overlays_entry_point_with_project_path(tmp_path: Path, config_db: Path) -> None:
    """Entry-point overlay gets project_path resolved from package location."""
    del config_db

    project_root = tmp_path / "myproject"
    _write_manage_py(project_root, "mypkg.contrib.settings")

    mock_ep = MagicMock()
    mock_ep.name = "my-overlay"
    mock_ep.value = "mypkg.contrib.settings"

    with (
        patch("importlib.metadata.entry_points", return_value=[mock_ep]),
        patch("teatree.config.discovery._resolve_ep_project_path", return_value=project_root),
    ):
        result = discover_overlays()
        assert len(result) == 1
        assert result[0].project_path == project_root


def test_definitionless_unregistered_overlay_is_not_discovered(config_db: Path) -> None:
    _seed_config_db(config_db, overlays={"teatree": {"mode": "auto"}})

    other_ep = MagicMock()
    other_ep.name = "t3-acme"
    other_ep.value = "acme_pkg.overlay:AcmeOverlay"
    real_ep = MagicMock()
    real_ep.name = "t3-teatree"
    real_ep.value = "teatree.contrib.t3_teatree.overlay:TeatreeOverlay"

    with (
        patch("importlib.metadata.entry_points", return_value=[other_ep, real_ep]),
        patch("teatree.config.discovery._resolve_ep_project_path", return_value=None),
    ):
        result = discover_overlays()

    names = {entry.name for entry in result}
    assert "t3-teatree" in names
    assert "teatree" not in names


# ── canonical_overlay_name (CLI route/dedup key) ─────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("t3-acme", "acme"),
        ("acme", "acme"),
        ("t3-beta", "beta"),
        ("beta", "beta"),
    ],
)
def test_canonical_overlay_name_strips_t3_prefix(name: str, expected: str) -> None:
    assert OverlayEntry.canonical_overlay_name(name) == expected


def test_short_registry_entry_is_ignored_when_only_canonical_entry_point_exists(config_db: Path) -> None:
    """Discovery admits the installed canonical name without using a short registry key."""
    _seed_config_db(config_db, overlays={"beta": {"mode": "auto"}})

    entry_point = MagicMock()
    entry_point.name = "t3-beta"
    entry_point.value = "beta_pkg.overlay:BetaOverlay"

    with (
        patch("importlib.metadata.entry_points", return_value=[entry_point]),
        patch("teatree.config.discovery._resolve_ep_project_path", return_value=None),
    ):
        result = discover_overlays()

    assert [entry.name for entry in result] == ["t3-beta"]
    assert result[0].overrides == {}
