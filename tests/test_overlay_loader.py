"""Tests for teatree.core.overlay_loader — TOML-based overlay discovery."""

from unittest.mock import patch

import teatree.config as config_mod
from teatree.config import TeaTreeConfig
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import _discover_toml_overlays


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


# ── Test helpers ─────────────────────────────────────────────────────


class _StubOverlay(OverlayBase):
    """Minimal concrete OverlayBase for testing."""

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree):
        return []


class _NotAnOverlay:
    """A class that does not subclass OverlayBase."""
