"""Tests for the backend loader."""

from typing import cast
from unittest.mock import MagicMock

import pytest

from teatree.backends.github import GitHubCodeHost
from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab_ci import GitLabCIService
from teatree.backends.loader import (
    get_ci_service,
    get_code_host,
    get_messaging,
    reset_backend_caches,
)
from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.backends.slack_bot import SlackBotBackend
from teatree.core.overlay import OverlayBase, OverlayConfig


def setup_function() -> None:
    reset_backend_caches()


def teardown_function() -> None:
    reset_backend_caches()


def _build_overlay(**config_kwargs: object) -> OverlayBase:
    overlay = MagicMock(spec=OverlayBase)
    config = OverlayConfig()
    for key, value in config_kwargs.items():
        setattr(config, key, value)
    overlay.config = config
    return cast("OverlayBase", overlay)


def _stub_token(overlay: OverlayBase, *, github: str = "", gitlab: str = "", slack: str = "") -> None:
    overlay.config.get_github_token = lambda: github  # type: ignore[method-assign]
    overlay.config.get_gitlab_token = lambda: gitlab  # type: ignore[method-assign]
    overlay.config.get_slack_token = lambda: slack  # type: ignore[method-assign]


def test_get_code_host_returns_none_when_no_token() -> None:
    overlay = _build_overlay()
    _stub_token(overlay)
    assert get_code_host(overlay) is None


def test_get_code_host_returns_github_when_explicit_choice() -> None:
    overlay = _build_overlay(code_host="github")
    _stub_token(overlay, github="gh-test-token")
    assert isinstance(get_code_host(overlay), GitHubCodeHost)


def test_get_code_host_returns_gitlab_when_explicit_choice() -> None:
    overlay = _build_overlay(code_host="gitlab")
    _stub_token(overlay, gitlab="gl-test-token")
    assert isinstance(get_code_host(overlay), GitLabCodeHost)


def test_get_code_host_falls_back_to_token_when_choice_unset() -> None:
    overlay = _build_overlay()
    _stub_token(overlay, gitlab="gl-test-token")
    assert isinstance(get_code_host(overlay), GitLabCodeHost)


def test_get_code_host_raises_on_unknown_choice() -> None:
    overlay = _build_overlay(code_host="bogus")
    _stub_token(overlay)
    with pytest.raises(ValueError, match="Unknown code_host"):
        get_code_host(overlay)


def test_get_messaging_default_is_noop() -> None:
    overlay = _build_overlay()
    _stub_token(overlay)
    assert isinstance(get_messaging(overlay), NoopMessagingBackend)


def test_get_messaging_returns_slack_when_chosen() -> None:
    overlay = _build_overlay(messaging_backend="slack")
    _stub_token(overlay, slack="xoxb-fake")
    assert isinstance(get_messaging(overlay), SlackBotBackend)


def test_get_messaging_raises_on_unknown_choice() -> None:
    overlay = _build_overlay(messaging_backend="bogus")
    _stub_token(overlay)
    with pytest.raises(ValueError, match="Unknown messaging_backend"):
        get_messaging(overlay)


def test_get_ci_service_returns_none_when_no_token() -> None:
    assert get_ci_service() is None


def test_get_ci_service_returns_gitlab_when_token_present() -> None:
    result = get_ci_service(gitlab_token="gl-test-token")
    assert isinstance(result, GitLabCIService)


def test_reset_backend_caches_clears_ci() -> None:
    reset_backend_caches()
    assert get_ci_service() is None
