"""Directory resolution, data/logging helpers and e2e-repo config.

Split verbatim from the former monolithic ``tests/test_config.py``
(souliane/teatree#443). Covers ``workspace_dir``/``worktrees_dir``
(Django-settings then config-file fallback), ``get_data_dir``,
``default_logging``, ``_extract_settings_module`` and ``load_e2e_repos``.

Integration-first per the Test-Writing Doctrine: real TOML fixtures
and a real on-disk ``manage.py`` under ``tmp_path``.
"""

from pathlib import Path

import pytest

from teatree.config import (
    E2ERepo,
    _extract_settings_module,
    clone_root,
    default_logging,
    get_data_dir,
    load_e2e_repos,
    worktree_root,
    worktrees_dir,
)

from ._shared import _write_toml


class TestWorktreeRoot:
    def test_returns_path_from_django_settings(self, tmp_path: Path, settings) -> None:
        custom = tmp_path / "custom-ws"
        settings.T3_WORKSPACE_DIR = str(custom)
        result = worktree_root()
        assert result == custom

    def test_falls_back_to_per_overlay_default(
        self, tmp_path: Path, config_file: Path, settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # workspace_dir is DB-home now: a ``[teatree] workspace_dir`` value is
        # IGNORED on read; with no env/Django override and no DB row the per-overlay
        # default ``~/workspace/t3-workspaces/<overlay>/`` stands. The DB and env
        # tiers are covered in test_workspace_dir_per_overlay.py.
        del config_file
        _write_toml(
            tmp_path / ".teatree.toml",
            '[teatree]\nworkspace_dir = "/from/config"\n',
        )
        if hasattr(settings, "T3_WORKSPACE_DIR"):
            del settings.T3_WORKSPACE_DIR
        monkeypatch.delenv("T3_WORKSPACE_DIR", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "myoverlay")

        assert worktree_root() == Path.home() / "workspace" / "t3-workspaces" / "myoverlay"


class TestCloneRoot:
    """The CLONE root (``~/workspace``) is DISTINCT from the per-overlay worktree root."""

    def test_returns_path_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", "/from/env/clones")
        assert clone_root() == Path("/from/env/clones")

    def test_returns_path_from_django_settings(self, settings, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_WORKSPACE_DIR", raising=False)
        settings.T3_WORKSPACE_DIR = "/from/django/clones"
        assert clone_root() == Path("/from/django/clones")

    def test_falls_back_to_home_workspace(self, settings, monkeypatch: pytest.MonkeyPatch) -> None:
        # No env, no Django override, no DB tier — the clone root is ``~/workspace``,
        # independent of the per-overlay worktree-root default.
        monkeypatch.delenv("T3_WORKSPACE_DIR", raising=False)
        if hasattr(settings, "T3_WORKSPACE_DIR"):
            del settings.T3_WORKSPACE_DIR
        assert clone_root() == Path.home() / "workspace"


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


def test_get_data_dir_creates_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.paths.DATA_DIR", tmp_path / "data")
    result = get_data_dir("test-namespace")
    assert result == tmp_path / "data" / "test-namespace"
    assert result.is_dir()


# ── default_logging ───────────────────────────────────────────────────


def test_default_logging_returns_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.paths.DATA_DIR", tmp_path / "data")
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


def test_load_e2e_repos_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        """
[e2e_repos.demo-svc]
url = "git@example.com:org/microservice-demo.git"
branch = "ac/demo-e2e"
e2e_dir = "e2e"
""",
    )
    repos = load_e2e_repos(config_path)
    assert len(repos) == 1
    assert repos[0].name == "demo-svc"
    assert repos[0].url == "git@example.com:org/microservice-demo.git"
    assert repos[0].branch == "ac/demo-e2e"
    assert repos[0].e2e_dir == "e2e"


def test_load_e2e_repos_missing_section(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, '[teatree]\nprivacy = "strict"\n')
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
