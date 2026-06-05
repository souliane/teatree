"""Tests for teatree.core.overlay_loader — TOML-based overlay discovery."""

import shutil
import subprocess
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import teatree.config as config_mod
from teatree.config import TeaTreeConfig
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import (
    _discover_toml_overlays,
    get_overlay_for_repo,
    infer_overlay_for_url,
    resolve_overlay_name,
)

_GIT = shutil.which("git") or "git"


def _make_config(overlays: dict) -> TeaTreeConfig:
    """Build a TeaTreeConfig whose raw dict contains the given overlays section."""
    return TeaTreeConfig(raw={"overlays": overlays})


class TestDiscoverTomlOverlaysSkip:
    """_discover_toml_overlays skips entries present in already_found."""

    def test_skips_already_found(self):
        config = _make_config(
            {"existing": {"class": "tests.test_overlay_loader:_StubOverlay"}},
        )
        with patch.object(config_mod, "load_config", return_value=config):
            result = _discover_toml_overlays(OverlayBase, {"existing"})
        assert result == {}

    def test_loads_entry_not_in_already_found(self):
        config = _make_config(
            {"new-overlay": {"class": "tests.test_overlay_loader:_StubOverlay"}},
        )
        with patch.object(config_mod, "load_config", return_value=config):
            result = _discover_toml_overlays(OverlayBase, set())
        assert "new-overlay" in result
        assert isinstance(result["new-overlay"], OverlayBase)


class TestDiscoverTomlOverlaysSuccess:
    """_discover_toml_overlays successfully instantiates a valid overlay class."""

    def test_loads_valid_overlay_class(self):
        config = _make_config(
            {"my-overlay": {"class": "tests.test_overlay_loader:_StubOverlay"}},
        )
        with patch.object(config_mod, "load_config", return_value=config):
            result = _discover_toml_overlays(OverlayBase, set())
        assert "my-overlay" in result
        assert isinstance(result["my-overlay"], _StubOverlay)


class TestDiscoverTomlOverlaysNotSubclass:
    """_discover_toml_overlays warns and skips when class is not a subclass."""

    def test_skips_non_subclass(self, caplog):
        config = _make_config(
            {"bad-overlay": {"class": "tests.test_overlay_loader:_NotAnOverlay"}},
        )
        with patch.object(config_mod, "load_config", return_value=config):
            result = _discover_toml_overlays(OverlayBase, set())
        assert result == {}
        assert "does not subclass OverlayBase" in caplog.text


class TestDiscoverTomlOverlaysImportError:
    """_discover_toml_overlays handles ImportError and AttributeError."""

    def test_handles_import_error(self, caplog):
        config = _make_config(
            {"missing-overlay": {"class": "nonexistent.module:SomeClass"}},
        )
        with patch.object(config_mod, "load_config", return_value=config):
            result = _discover_toml_overlays(OverlayBase, set())
        assert result == {}
        assert "failed to load class" in caplog.text

    def test_handles_attribute_error(self, caplog):
        config = _make_config(
            {"missing-attr": {"class": "tests.test_overlay_loader:NoSuchClass"}},
        )
        with patch.object(config_mod, "load_config", return_value=config):
            result = _discover_toml_overlays(OverlayBase, set())
        assert result == {}
        assert "failed to load class" in caplog.text


class TestInferOverlayForUrl:
    """``infer_overlay_for_url`` maps a URL to the owning overlay (#743)."""

    def _overlay(self, repos: list[str]):
        class _Cfg:
            workspace_repos: ClassVar[list[str]] = []

        class _Overlay:
            config = _Cfg()

            def get_workspace_repos(self) -> list[str]:
                return repos

        return _Overlay()

    def test_empty_url_returns_empty(self):
        assert infer_overlay_for_url("") == ""

    def test_matches_via_get_workspace_repos(self):
        with patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"gl": self._overlay(["acme/widgets"])},
        ):
            assert infer_overlay_for_url("https://gitlab.com/acme/widgets/-/issues/7") == "gl"

    def test_no_match_returns_empty(self):
        with patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"gl": self._overlay(["other/repo"])},
        ):
            assert infer_overlay_for_url("https://gitlab.com/acme/widgets/-/issues/7") == ""

    def test_non_overlay_entry_is_skipped(self):
        class _Bare:
            config = None

        with patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"bare": _Bare()},
        ):
            assert infer_overlay_for_url("https://example.com/x/issues/1") == ""

    def test_raising_overlay_does_not_block_others(self, caplog):
        class _Broken:
            def get_workspace_repos(self) -> list[str]:
                msg = "boom"
                raise RuntimeError(msg)

        with patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"broken": _Broken(), "ok": self._overlay(["acme/widgets"])},
        ):
            assert infer_overlay_for_url("https://gitlab.com/acme/widgets/-/issues/7") == "ok"
        assert "failed during inference" in caplog.text


def _init_repo_with_origin(path: Path, origin_url: str) -> None:
    """Create a real git repo at ``path`` with ``origin`` set to ``origin_url``."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run([_GIT, "init", "-q"], cwd=path, check=True)
    subprocess.run([_GIT, "remote", "add", "origin", origin_url], cwd=path, check=True)


class _RepoOverlay(OverlayBase):
    """Concrete overlay exposing a fixed workspace-repo slug list."""

    def __init__(self, repos: list[str]) -> None:
        self._repos = repos

    def get_repos(self) -> list[str]:
        return self._repos

    def get_provision_steps(self, worktree):
        return []


class TestGetOverlayForRepo:
    """``get_overlay_for_repo`` maps the cwd git repo to its owning overlay (#1526).

    Resolves the overlay deterministically by the ``origin`` remote slug of
    the repo at the given path, matched against each registered overlay's
    ``get_workspace_repos()``. Returns ``None`` when the slug matches zero or
    more than one overlay so the caller can fall back without crashing.
    """

    def test_matches_repo_to_its_owning_overlay(self, tmp_path):
        repo = tmp_path / "widgets"
        _init_repo_with_origin(repo, "git@github.com:acme/widgets.git")
        overlays = {
            "a": _RepoOverlay(["acme/widgets"]),
            "b": _RepoOverlay(["other/repo"]),
        }
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            resolved = get_overlay_for_repo(str(repo))
        assert resolved is overlays["a"]

    def test_no_match_returns_none(self, tmp_path):
        repo = tmp_path / "ghost"
        _init_repo_with_origin(repo, "git@github.com:acme/ghost.git")
        overlays = {
            "a": _RepoOverlay(["acme/widgets"]),
            "b": _RepoOverlay(["other/repo"]),
        }
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert get_overlay_for_repo(str(repo)) is None

    def test_ambiguous_match_returns_none(self, tmp_path):
        repo = tmp_path / "shared"
        _init_repo_with_origin(repo, "git@github.com:acme/shared.git")
        overlays = {
            "a": _RepoOverlay(["acme/shared"]),
            "b": _RepoOverlay(["acme/shared"]),
        }
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert get_overlay_for_repo(str(repo)) is None

    def test_repo_without_origin_returns_none(self, tmp_path):
        repo = tmp_path / "no-origin"
        repo.mkdir()
        subprocess.run([_GIT, "init", "-q"], cwd=repo, check=True)
        overlays = {"a": _RepoOverlay(["acme/widgets"])}
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert get_overlay_for_repo(str(repo)) is None

    def test_non_overlay_entry_is_skipped(self, tmp_path):
        repo = tmp_path / "widgets"
        _init_repo_with_origin(repo, "git@github.com:acme/widgets.git")

        class _Bare:
            config = None

        overlays = {"bare": _Bare(), "a": _RepoOverlay(["acme/widgets"])}
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert get_overlay_for_repo(str(repo)) is overlays["a"]

    def test_raising_overlay_does_not_block_others(self, tmp_path, caplog):
        repo = tmp_path / "widgets"
        _init_repo_with_origin(repo, "git@github.com:acme/widgets.git")

        class _Broken(OverlayBase):
            def get_repos(self) -> list[str]:
                msg = "boom"
                raise RuntimeError(msg)

            def get_workspace_repos(self) -> list[str]:
                msg = "boom"
                raise RuntimeError(msg)

            def get_provision_steps(self, worktree):
                return []

        overlays = {"broken": _Broken(), "a": _RepoOverlay(["acme/widgets"])}
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert get_overlay_for_repo(str(repo)) is overlays["a"]
        assert "failed during repo resolution" in caplog.text


class TestResolveOverlayName:
    """``resolve_overlay_name`` folds a name onto its registered canonical form (#1959)."""

    def test_registered_name_resolves_to_itself(self):
        with patch(
            "teatree.core.overlay_loader.get_all_overlay_names",
            return_value=["t3-teatree", "t3-beta"],
        ):
            assert resolve_overlay_name("t3-teatree") == "t3-teatree"

    def test_legacy_short_alias_folds_onto_entry_point(self):
        with patch(
            "teatree.core.overlay_loader.get_all_overlay_names",
            return_value=["t3-teatree", "t3-beta"],
        ):
            assert resolve_overlay_name("teatree") == "t3-teatree"
            assert resolve_overlay_name("beta") == "t3-beta"

    def test_unknown_name_resolves_to_none(self):
        with patch(
            "teatree.core.overlay_loader.get_all_overlay_names",
            return_value=["t3-teatree", "t3-beta"],
        ):
            assert resolve_overlay_name("removed-overlay") is None
            assert resolve_overlay_name("synthetic-tag") is None
            assert resolve_overlay_name("a-multi-segment-stale-name") is None

    def test_empty_name_resolves_to_none(self):
        assert resolve_overlay_name("") is None

    def test_dispatchable_check_via_resolution(self):
        with patch(
            "teatree.core.overlay_loader.get_all_overlay_names",
            return_value=["t3-teatree", "t3-beta"],
        ):
            assert resolve_overlay_name("teatree") is not None
            assert resolve_overlay_name("removed-overlay") is None


# ── Test helpers ─────────────────────────────────────────────────────


class _StubOverlay(OverlayBase):
    """Minimal concrete OverlayBase for testing."""

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree):
        return []


class _NotAnOverlay:
    """A class that does not subclass OverlayBase."""
