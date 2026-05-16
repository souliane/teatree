"""Tests for teatree.core.overlay_loader — TOML-based overlay discovery."""

from typing import ClassVar
from unittest.mock import patch

import teatree.config as config_mod
from teatree.config import TeaTreeConfig
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import _discover_toml_overlays, infer_overlay_for_url


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


# ── Test helpers ─────────────────────────────────────────────────────


class _StubOverlay(OverlayBase):
    """Minimal concrete OverlayBase for testing."""

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree):
        return []


class _NotAnOverlay:
    """A class that does not subclass OverlayBase."""
