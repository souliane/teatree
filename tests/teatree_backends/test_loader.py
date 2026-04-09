"""Tests for the backend loader (no overlay dependency)."""

from teatree.backends.github import GitHubCodeHost
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


def setup_function() -> None:
    reset_backend_caches()


def teardown_function() -> None:
    reset_backend_caches()


def test_get_code_host_returns_none_when_no_token() -> None:
    assert get_code_host() is None


def test_get_code_host_returns_github_when_token_present() -> None:
    result = get_code_host(github_token="gh-test-token")
    assert isinstance(result, GitHubCodeHost)


def test_get_code_host_returns_gitlab_when_token_present() -> None:
    result = get_code_host(gitlab_token="gl-test-token")
    assert isinstance(result, GitLabCodeHost)


def test_get_issue_tracker_returns_none() -> None:
    assert get_issue_tracker() is None


def test_get_chat_notifier_returns_none() -> None:
    assert get_chat_notifier() is None


def test_get_error_tracker_returns_none() -> None:
    assert get_error_tracker() is None


def test_get_ci_service_returns_none_when_no_token() -> None:
    assert get_ci_service() is None


def test_get_ci_service_returns_gitlab_when_token_present() -> None:
    result = get_ci_service(gitlab_token="gl-test-token")
    assert isinstance(result, GitLabCIService)


def test_reset_backend_caches_clears_all() -> None:
    reset_backend_caches()
    assert get_code_host() is None
    assert get_issue_tracker() is None
    assert get_chat_notifier() is None
    assert get_error_tracker() is None
    assert get_ci_service() is None
