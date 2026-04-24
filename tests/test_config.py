"""Tests for overlay discovery from ~/.teatree.toml and entry points.

Integration-first per the Test-Writing Doctrine: real TOML fixtures under
``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched to them.
Mocks are reserved for unstoppable externals: the ``gh`` CLI call in
``check_for_updates`` (network) and ``importlib.metadata.entry_points``
(represents installed overlay packages — otherwise we'd have to install
fixture packages per test).
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.config import (
    E2ERepo,
    Mode,
    _extract_settings_module,
    _resolve_ep_project_path,
    _write_update_cache,
    check_for_updates,
    default_logging,
    discover_active_overlay,
    discover_overlays,
    get_data_dir,
    get_effective_settings,
    load_config,
    load_e2e_repos,
    workspace_dir,
    worktrees_dir,
)


def _write_manage_py(project_path: Path, settings_module: str = "myapp.settings") -> None:
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "manage.py").write_text(f'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{settings_module}")\n')


def _write_toml(config_path: Path, content: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stage a real ``~/.teatree.toml`` under ``tmp_path`` and wire it to the module."""
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    return cfg


@pytest.fixture
def no_installed_overlays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``importlib.metadata.entry_points`` report no teatree overlays.

    Installed teatree entry points (``t3-teatree``) would otherwise leak
    into overlay discovery and shadow the TOML fixtures.
    """
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the test from a cwd free of ``manage.py`` ancestors."""
    away = tmp_path / "no_manage"
    away.mkdir()
    monkeypatch.chdir(away)
    return away


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


# ── load_config ───────────────────────────────────────────────────────


def test_load_config_from_file(tmp_path: Path) -> None:
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


def test_load_config_missing_file(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nonexistent.toml")
    assert config.user.workspace_dir == Path.home() / "workspace"
    assert config.user.branch_prefix == ""
    assert config.user.privacy == ""


def test_load_config_defaults_when_teatree_section_empty(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[other]\nfoo = 1\n")
    config = load_config(config_path)
    assert config.user.branch_prefix == ""


# ── get_data_dir ──────────────────────────────────────────────────────


def test_get_data_dir_creates_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.config.DATA_DIR", tmp_path / "data")
    result = get_data_dir("test-namespace")
    assert result == tmp_path / "data" / "test-namespace"
    assert result.is_dir()


# ── default_logging ───────────────────────────────────────────────────


def test_default_logging_returns_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.config.DATA_DIR", tmp_path / "data")
    config = default_logging("test-ns")
    assert config["version"] == 1
    assert "file" in config["handlers"]
    assert "console" in config["handlers"]
    log_dir = tmp_path / "data" / "test-ns" / "logs"
    assert log_dir.is_dir()


# ── _extract_settings_module ──────────────────────────────────────────


def test_extract_settings_module_found(tmp_path: Path) -> None:
    manage_py = tmp_path / "manage.py"
    manage_py.write_text('os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myapp.settings")\n')
    assert _extract_settings_module(manage_py) == "myapp.settings"


def test_extract_settings_module_not_found(tmp_path: Path) -> None:
    manage_py = tmp_path / "manage.py"
    manage_py.write_text("#!/usr/bin/env python\npass\n")
    assert _extract_settings_module(manage_py) == ""


# ── discover_active_overlay ──────────────────────────────────────────


def test_discover_active_overlay_from_manage_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """discover_active_overlay finds overlay from manage.py in cwd ancestors."""
    _write_manage_py(tmp_path, "active.settings")
    monkeypatch.chdir(tmp_path)
    result = discover_active_overlay()
    assert result is not None
    assert result.project_path == tmp_path


def test_discover_active_overlay_single_installed(
    config_file: Path, elsewhere: Path, no_installed_overlays: None
) -> None:
    """Single TOML-declared overlay (no manage.py in cwd ancestors) is picked as active."""
    del elsewhere, no_installed_overlays
    _write_toml(config_file, '[overlays.acme]\nclass = "acme.settings"\n')

    result = discover_active_overlay()

    assert result is not None
    assert result.name == "acme"


def test_discover_active_overlay_none_when_multiple(
    config_file: Path, elsewhere: Path, no_installed_overlays: None
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
    config_file: Path, elsewhere: Path, no_installed_overlays: None
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_installed_overlays: None
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


# ── workspace_dir / worktrees_dir ────────────────────────────────────


class TestWorkspaceDir:
    def test_returns_path_from_django_settings(self, tmp_path: Path, settings) -> None:
        custom = tmp_path / "custom-ws"
        settings.T3_WORKSPACE_DIR = str(custom)
        result = workspace_dir()
        assert result == custom

    def test_falls_back_to_config_file(self, tmp_path: Path, config_file: Path, settings) -> None:
        del config_file
        _write_toml(
            tmp_path / ".teatree.toml",
            '[teatree]\nworkspace_dir = "/from/config"\n',
        )
        if hasattr(settings, "T3_WORKSPACE_DIR"):
            del settings.T3_WORKSPACE_DIR

        assert workspace_dir() == Path("/from/config")


class TestWorktreesDir:
    def test_returns_path_from_django_settings(self, tmp_path: Path, settings) -> None:
        custom = tmp_path / "custom-wt"
        settings.T3_WORKTREES_DIR = str(custom)
        result = worktrees_dir()
        assert result == custom

    def test_falls_back_to_config_file(self, tmp_path: Path, config_file: Path, settings) -> None:
        del config_file
        _write_toml(
            tmp_path / ".teatree.toml",
            '[teatree]\nworktrees_dir = "/from/config/wt"\n',
        )
        if hasattr(settings, "T3_WORKTREES_DIR"):
            del settings.T3_WORKTREES_DIR

        assert worktrees_dir() == Path("/from/config/wt")


# ── check_for_updates ────────────────────────────────────────────────


def _write_check_updates_toml(config_file: Path, *, enabled: bool) -> None:
    _write_toml(config_file, f"[teatree]\ncheck_updates = {'true' if enabled else 'false'}\n")


class TestCheckForUpdates:
    def test_returns_none_when_updates_disabled(self, config_file: Path) -> None:
        """Early return None when check_updates=false and force=False."""
        _write_check_updates_toml(config_file, enabled=False)

        assert check_for_updates(force=False) is None

    def test_cached_result_returned_when_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path
    ) -> None:
        """Return cached message when within TTL."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cache_path = data_dir / "update-check.json"
        cache_path.write_text(
            json.dumps({"ts": time.time(), "message": "teatree v9.9 available"}),
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        assert check_for_updates(force=False) == "teatree v9.9 available"

    def test_cached_empty_message_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path
    ) -> None:
        """Cached empty message means up-to-date => None."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cache_path = data_dir / "update-check.json"
        cache_path.write_text(
            json.dumps({"ts": time.time(), "message": ""}),
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        assert check_for_updates(force=False) is None

    def test_cached_corrupt_json_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path
    ) -> None:
        """Corrupt cache JSON is silently ignored, proceeds to network check."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cache_path = data_dir / "update-check.json"
        cache_path.write_text("NOT VALID JSON {{{", encoding="utf-8")
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="v1.0.0\n")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            # Falls through corrupt cache, hits network, finds same version
            assert check_for_updates(force=False) is None

    def test_empty_tag_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
        """When gh returns empty tag, returns None."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="\n")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            assert check_for_updates(force=True) is None

    def test_subprocess_timeout_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path
    ) -> None:
        """TimeoutExpired from gh CLI returns None."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 10)):
            assert check_for_updates(force=True) is None

    def test_file_not_found_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path
    ) -> None:
        """FileNotFoundError (gh not installed) returns None."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert check_for_updates(force=True) is None

    def test_newer_version_returns_upgrade_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path
    ) -> None:
        """When latest != current, returns upgrade message."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="v2.0.0\n")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            result = check_for_updates(force=True)

        assert result is not None
        assert "v2.0.0" in result
        assert "1.0.0" in result
        assert "uv pip install --upgrade teatree" in result

        cache_path = data_dir / "update-check.json"
        assert cache_path.is_file()
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert "v2.0.0" in cached["message"]

    def test_same_version_returns_none_and_caches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path
    ) -> None:
        """When latest == current, returns None and caches empty."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.config.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="v1.0.0\n")
        with (
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
    def test_creates_parent_dirs_and_writes_json(self, tmp_path: Path) -> None:
        """Creates parent dirs and writes valid JSON cache."""
        cache_path = tmp_path / "nested" / "dir" / "update-check.json"
        _write_update_cache(cache_path, "test message")

        assert cache_path.is_file()
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert data["message"] == "test message"
        assert "ts" in data
        assert isinstance(data["ts"], float)


# ── load_e2e_repos ────────────────────────────────────────────────────


def test_load_e2e_repos_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        """
[e2e_repos.home-savings]
url = "git@gitlab.com:org/microservice-home-savings.git"
branch = "ac/mhs-e2e"
e2e_dir = "e2e"
""",
    )
    repos = load_e2e_repos(config_path)
    assert len(repos) == 1
    assert repos[0].name == "home-savings"
    assert repos[0].url == "git@gitlab.com:org/microservice-home-savings.git"
    assert repos[0].branch == "ac/mhs-e2e"
    assert repos[0].e2e_dir == "e2e"


def test_load_e2e_repos_missing_section(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, '[teatree]\nbranch_prefix = "ac-"\n')
    assert load_e2e_repos(config_path) == []


def test_load_e2e_repos_default_e2e_dir(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        """
[e2e_repos.my-service]
url = "git@github.com:org/my-service.git"
branch = "feature/e2e"
""",
    )
    repos = load_e2e_repos(config_path)
    assert repos[0].e2e_dir == "e2e"


def test_load_e2e_repos_multiple(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        """
[e2e_repos.service-a]
url = "git@github.com:org/service-a.git"
branch = "main"

[e2e_repos.service-b]
url = "git@github.com:org/service-b.git"
branch = "feature/tests"
e2e_dir = "playwright"
""",
    )
    repos = load_e2e_repos(config_path)
    by_name = {r.name: r for r in repos}
    assert set(by_name) == {"service-a", "service-b"}
    assert by_name["service-b"].e2e_dir == "playwright"


def test_load_e2e_repos_missing_toml(tmp_path: Path) -> None:
    assert load_e2e_repos(tmp_path / "nonexistent.toml") == []


def test_e2e_repo_is_dataclass() -> None:
    repo = E2ERepo(name="x", url="u", branch="b")
    assert repo.name == "x"
    assert repo.e2e_dir == "e2e"  # default


# ── Mode / operating mode ────────────────────────────────────────────


class TestMode:
    """Parse and resolution of the ``t3.mode`` setting.

    The default must stay conservative (INTERACTIVE): auto mode grants the
    agent end-to-end autonomy including publishing actions, so a typo in the
    config must never silently downgrade to it.
    """

    def test_parse_interactive(self) -> None:
        assert Mode.parse("interactive") is Mode.INTERACTIVE

    def test_parse_auto(self) -> None:
        assert Mode.parse("auto") is Mode.AUTO

    def test_parse_is_case_insensitive(self) -> None:
        assert Mode.parse("AUTO") is Mode.AUTO
        assert Mode.parse("  Interactive  ") is Mode.INTERACTIVE

    def test_parse_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid t3 mode"):
            Mode.parse("headless")

    def test_load_config_default_is_interactive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_MODE", raising=False)
        config = load_config(tmp_path / "nonexistent.toml")
        assert config.user.mode is Mode.INTERACTIVE

    def test_load_config_reads_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_MODE", raising=False)
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nmode = "auto"\n')
        config = load_config(config_path)
        assert config.user.mode is Mode.AUTO

    def test_env_var_overrides_toml(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """T3_MODE wins over the toml global — verified via get_effective_settings."""
        del elsewhere, no_installed_overlays
        _write_toml(config_file, '[teatree]\nmode = "auto"\n')
        monkeypatch.setenv("T3_MODE", "interactive")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

        assert get_effective_settings().mode is Mode.INTERACTIVE

    def test_env_var_applies_without_toml(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """T3_MODE applies even when no toml file exists."""
        del config_file, elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_MODE", "auto")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

        assert get_effective_settings().mode is Mode.AUTO

    def test_load_config_invalid_mode_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_MODE", raising=False)
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nmode = "headless"\n')

        with pytest.raises(ValueError, match="Invalid t3 mode"):
            load_config(config_path)

    def test_get_mode_reflects_loaded_config(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

        _write_toml(config_file, '[teatree]\nmode = "auto"\n')
        assert get_effective_settings().mode is Mode.AUTO

        _write_toml(config_file, '[teatree]\nmode = "interactive"\n')
        assert get_effective_settings().mode is Mode.INTERACTIVE


# ── per-overlay override machinery ───────────────────────────────────


class TestOverlayOverrides:
    """Per-overlay overrides for any key in ``OVERLAY_OVERRIDABLE_SETTINGS``.

    The resolution chain is env (where applicable) → active overlay override
    → global → dataclass default. The active overlay is picked via
    ``T3_OVERLAY_NAME`` when set, else cwd-based discovery.
    """

    def test_overlay_toml_mode_parsed(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(
            config_path,
            """
[teatree]
mode = "interactive"

[overlays.my-overlay]
class = "x.y:Z"
mode = "auto"
""",
        )
        entries = discover_overlays(config_path=config_path)
        by_name = {e.name: e for e in entries}
        assert by_name["my-overlay"].overrides["mode"] is Mode.AUTO

    def test_overlay_invalid_mode_raises(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(
            config_path,
            """
[overlays.my-overlay]
class = "x.y:Z"
mode = "nope"
""",
        )
        with pytest.raises(ValueError, match="Invalid t3 mode"):
            discover_overlays(config_path=config_path)

    def test_overlay_override_wins_over_global(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")

        _write_toml(
            config_file,
            """
[teatree]
mode = "interactive"
branch_prefix = "ac"

[overlays.my-overlay]
class = "x.y:Z"
mode = "auto"
branch_prefix = "xp"
""",
        )

        effective = get_effective_settings()
        assert effective.mode is Mode.AUTO
        assert effective.branch_prefix == "xp"

    def test_overlay_without_override_inherits_global(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "x")

        _write_toml(
            config_file,
            """
[teatree]
mode = "auto"

[overlays.x]
class = "x.y:Z"
""",
        )

        assert get_effective_settings().mode is Mode.AUTO

    def test_env_var_beats_overlay_override(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_MODE", "interactive")
        monkeypatch.setenv("T3_OVERLAY_NAME", "x")

        _write_toml(
            config_file,
            """
[teatree]
mode = "interactive"

[overlays.x]
class = "x.y:Z"
mode = "auto"
""",
        )

        assert get_effective_settings().mode is Mode.INTERACTIVE

    def test_t3_overlay_name_selects_entry(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "b")

        _write_toml(
            config_file,
            """
[teatree]
mode = "interactive"

[overlays.a]
class = "x"
mode = "interactive"

[overlays.b]
class = "y"
mode = "auto"
""",
        )

        assert get_effective_settings().mode is Mode.AUTO
