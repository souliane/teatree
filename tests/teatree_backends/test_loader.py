"""Tests for the overlay-driven backend loader."""

from unittest.mock import patch

from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab_ci import GitLabCIService
from teatree.backends.loader import (
    get_chat_notifier,
    get_ci_service,
    get_code_host,
    get_error_tracker,
    get_issue_tracker,
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
    return patch(
        "teatree.core.overlay_loader._discover_overlays",
        return_value={"test": overlay_cls()},
    )


def test_get_code_host_returns_none_when_no_token() -> None:
    with _patch_overlay(_NoTokenOverlay):
        assert get_code_host() is None


def test_get_code_host_returns_gitlab_when_token_present() -> None:
    with _patch_overlay(_TokenOverlay):
        result = get_code_host()
        assert isinstance(result, GitLabCodeHost)


def test_get_issue_tracker_returns_none() -> None:
    assert get_issue_tracker() is None


def test_get_chat_notifier_returns_none() -> None:
    assert get_chat_notifier() is None


def test_get_error_tracker_returns_none() -> None:
    assert get_error_tracker() is None


def test_get_ci_service_returns_none_when_no_token() -> None:
    with _patch_overlay(_NoTokenOverlay):
        assert get_ci_service() is None


def test_get_ci_service_returns_gitlab_when_token_present() -> None:
    with _patch_overlay(_TokenOverlay):
        result = get_ci_service()
        assert isinstance(result, GitLabCIService)


def test_get_code_host_returns_none_when_overlay_not_configured() -> None:
    with patch(
        "teatree.core.overlay_loader._discover_overlays",
        return_value={},
    ):
        assert get_code_host() is None


def test_get_ci_service_returns_none_when_overlay_not_configured() -> None:
    with patch(
        "teatree.core.overlay_loader._discover_overlays",
        return_value={},
    ):
        assert get_ci_service() is None


def test_reset_backend_caches_clears_all() -> None:
    reset_backend_caches()
    with _patch_overlay(_NoTokenOverlay):
        assert get_code_host() is None
        assert get_issue_tracker() is None
        assert get_chat_notifier() is None
        assert get_error_tracker() is None
        assert get_ci_service() is None
