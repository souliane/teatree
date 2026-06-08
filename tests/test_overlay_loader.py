"""Tests for teatree.core.overlay_loader — TOML-based overlay discovery."""

import shutil
import subprocess
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

import teatree.config as config_mod
from teatree.config import TeaTreeConfig
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import (
    _discover_toml_overlays,
    frontend_repos_for_overlay,
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

    def test_bare_relative_path_does_not_own_sibling_slug_url(self):
        """A bare relative path (``t3-company``) must not own a sibling clone's URL (#1120).

        ``t3-teatree.get_workspace_repos()`` falls back to
        ``_discover_workspace_repos()``, which emits each discovered overlay
        as a workspace-RELATIVE bare path (``"t3-company"``), not an
        ``owner/name`` slug. With a raw-substring match, the bare token
        ``"t3-company"`` is a substring of the reporter's URL
        ``.../company-fork-org/t3-company/issues/147`` and the first
        dict-iteration hit (``t3-teatree``) wins — poisoning the ticket's
        overlay attribution. The real owner is ``t3-company``, whose
        ``get_workspace_repos()`` carries the proper ``owner/name`` slug.
        """
        url = "https://github.com/company-fork-org/t3-company/issues/147"
        overlays = {
            "t3-teatree": self._overlay(["teatree", "t3-company"]),
            "t3-company": self._overlay(["company-fork-org/t3-company"]),
        }
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert infer_overlay_for_url(url) == "t3-company"

        # Guard against dict-iteration order: reversing the insertion order
        # must still resolve the true owner (the slug match wins, not the
        # first hit).
        reversed_overlays = {
            "t3-company": self._overlay(["company-fork-org/t3-company"]),
            "t3-teatree": self._overlay(["teatree", "t3-company"]),
        }
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=reversed_overlays):
            assert infer_overlay_for_url(url) == "t3-company"

    def test_ambiguous_match_returns_empty(self):
        """Two overlays both owning the URL fail safe to ``""`` (#1120).

        Returning the first dict hit would silently bind the ticket to an
        arbitrary one of the two. ``""`` instead routes ``get_overlay(None)``
        to the explicit ``ImproperlyConfigured`` listing installed overlays,
        so the operator passes ``T3_OVERLAY_NAME`` rather than getting a
        wrong-but-nonempty attribution.
        """
        url = "https://github.com/company-fork-org/t3-company/issues/147"
        overlays = {
            "first": self._overlay(["company-fork-org/t3-company"]),
            "second": self._overlay(["company-fork-org/t3-company"]),
        }
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            assert infer_overlay_for_url(url) == ""

    def test_segment_boundary_prevents_prefix_collision(self):
        """``acme/widget`` must not own ``acme/widget-extra`` (#1120).

        The slug ``acme/widget`` IS a raw substring of
        ``.../acme/widget-extra/issues/1``, so the old matcher wrongly
        claimed the URL. A segment-boundary match rejects it: the trailing
        path segment ``widget`` does not equal ``widget-extra``.
        """
        url = "https://github.com/acme/widget-extra/issues/1"
        with patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"a": self._overlay(["acme/widget"])},
        ):
            assert infer_overlay_for_url(url) == ""

    def test_gitlab_subgroup_slug_resolves(self):
        """A GitLab subgroup ``owner/name`` slug still resolves its URL (#1120).

        The boundary-aware match must not regress GitLab subgroup paths
        (``group/subgroup/repo``) — the full project path parses correctly
        and the equal slug wins.
        """
        url = "https://gitlab.com/group/subgroup/repo/-/issues/3"
        with patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"gl": self._overlay(["group/subgroup/repo"])},
        ):
            assert infer_overlay_for_url(url) == "gl"


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


class TestFrontendReposForOverlay:
    """``frontend_repos_for_overlay`` resolves path-only TOML overlays (#733).

    A path-only overlay (``path`` but no Python ``class``) is reached through
    the CLI subprocess bridge and cannot be instantiated as ``OverlayBase`` in
    the teatree process, so ``get_overlay`` raises ``Overlay not found`` for
    it. Before this helper, an in-process safety gate (the DoD local-E2E gate)
    therefore failed CLOSED for EVERY ticket of such an overlay. The helper
    answers from the overlay's ``[overlays.<name>]`` TOML table instead, while
    keeping the genuinely-unknown overlay raising so the gate's fail-closed
    posture survives where it is actually warranted.
    """

    def _patch_landscape(self, overlays: dict, discovered: dict | None):
        """Patch the entry-point/TOML discovery landscape (the unstoppable external)."""
        from contextlib import ExitStack  # noqa: PLC0415

        stack = ExitStack()
        stack.enter_context(patch.object(config_mod, "load_config", return_value=_make_config(overlays)))
        stack.enter_context(patch("teatree.core.overlay_loader._discover_overlays", return_value=discovered or {}))
        return stack

    def test_path_only_overlay_with_no_frontend_repos_resolves_empty(self):
        """The regression: a path-only overlay must resolve to ``[]``, not raise."""
        overlays = {"t3-path": {"path": "~/somewhere/t3-path", "protected_branches": ["development"]}}
        with self._patch_landscape(overlays, discovered={}):
            assert frontend_repos_for_overlay("t3-path") == []

    def test_path_only_overlay_with_declared_frontend_repos_resolves_them(self):
        overlays = {"t3-path": {"path": "~/x/t3-path", "frontend_repos": ["acme-web", "acme-admin"]}}
        with self._patch_landscape(overlays, discovered={}):
            assert frontend_repos_for_overlay("t3-path") == ["acme-web", "acme-admin"]

    def test_instantiable_overlay_answers_from_its_config(self):
        overlay = _StubOverlay()
        overlay.config.frontend_repos = ["from-config"]
        with self._patch_landscape({}, discovered={"t3-stub": overlay}):
            assert frontend_repos_for_overlay("t3-stub") == ["from-config"]

    def test_genuinely_unknown_overlay_raises_for_fail_closed(self):
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

        with self._patch_landscape({}, discovered={}), pytest.raises(ImproperlyConfigured):
            frontend_repos_for_overlay("removed-or-typo")


# ── Test helpers ─────────────────────────────────────────────────────


class _StubOverlay(OverlayBase):
    """Minimal concrete OverlayBase for testing."""

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree):
        return []


class _NotAnOverlay:
    """A class that does not subclass OverlayBase."""
