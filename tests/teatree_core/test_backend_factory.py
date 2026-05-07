"""Tests for the overlay-aware backend factory bridge."""

from unittest.mock import patch

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab_ci import GitLabCIService
from teatree.core.backend_factory import (
    ci_service_from_overlay,
    code_host_from_overlay,
    messaging_from_overlay,
    reset_backend_caches,
)
from teatree.core.overlay import OverlayBase, OverlayConfig


class _TokenConfig(OverlayConfig):
    def get_gitlab_token(self) -> str:
        return "gl-test-token"


class _TokenOverlay(OverlayBase):
    config = _TokenConfig()

    def get_repos(self):
        return []

    def get_provision_steps(self, worktree):
        return []


class _NoTokenOverlay(OverlayBase):
    def get_repos(self):
        return []

    def get_provision_steps(self, worktree):
        return []


def setup_function() -> None:
    reset_backend_caches()


def teardown_function() -> None:
    reset_backend_caches()


def _patch_overlay(overlay_cls):
    return patch.object(
        overlay_loader_mod,
        "_discover_overlays",
        return_value={"test": overlay_cls()},
    )


def test_code_host_from_overlay_returns_none_when_no_token() -> None:
    with _patch_overlay(_NoTokenOverlay):
        assert code_host_from_overlay() is None


def test_code_host_from_overlay_returns_gitlab_when_token_present() -> None:
    with _patch_overlay(_TokenOverlay):
        result = code_host_from_overlay()
        assert isinstance(result, GitLabCodeHost)


def test_ci_service_from_overlay_returns_none_when_no_token() -> None:
    with _patch_overlay(_NoTokenOverlay):
        assert ci_service_from_overlay() is None


def test_ci_service_from_overlay_returns_gitlab_when_token_present() -> None:
    with _patch_overlay(_TokenOverlay):
        result = ci_service_from_overlay()
        assert isinstance(result, GitLabCIService)


def test_code_host_from_overlay_returns_none_when_overlay_not_configured() -> None:
    with patch.object(
        overlay_loader_mod,
        "_discover_overlays",
        return_value={},
    ):
        assert code_host_from_overlay() is None


def test_ci_service_from_overlay_returns_none_when_overlay_not_configured() -> None:
    with patch.object(
        overlay_loader_mod,
        "_discover_overlays",
        return_value={},
    ):
        assert ci_service_from_overlay() is None


def test_messaging_from_overlay_returns_none_when_overlay_not_configured() -> None:
    with patch.object(
        overlay_loader_mod,
        "_discover_overlays",
        return_value={},
    ):
        assert messaging_from_overlay() is None


def test_messaging_from_overlay_delegates_to_loader() -> None:
    with (
        _patch_overlay(_NoTokenOverlay),
        patch("teatree.core.backend_factory.get_messaging", return_value="sentinel") as get_messaging_mock,
    ):
        result = messaging_from_overlay()

    assert result == "sentinel"
    get_messaging_mock.assert_called_once()


def test_reset_backend_caches_clears_all_caches() -> None:
    with _patch_overlay(_TokenOverlay):
        first = code_host_from_overlay()
    reset_backend_caches()
    with _patch_overlay(_NoTokenOverlay):
        second = code_host_from_overlay()
    assert first is not second
