"""Tests for the settings-driven backend loader."""

from django.test import override_settings

from teatree.backends.loader import (
    _load_backend,
    get_chat_notifier,
    get_ci_service,
    get_code_host,
    get_error_tracker,
    get_issue_tracker,
    reset_backend_caches,
)


class _DummyCodeHost:
    def create_pr(self, *, repo, branch, title, description, target_branch=""):
        return {}

    def list_open_prs(self, repo, author):
        return []

    def post_mr_note(self, *, repo, mr_iid, body):
        return {}


class _DummyIssueTracker:
    def get_issue(self, issue_url):
        return {}


class _DummyChatNotifier:
    def send(self, *, channel, text):
        return {}


class _DummyErrorTracker:
    def get_top_issues(self, *, project, limit=10):
        return []


class _DummyCIService:
    def cancel_pipelines(self, *, project, ref):
        return []

    def fetch_pipeline_errors(self, *, project, ref):
        return []

    def fetch_failed_tests(self, *, project, ref):
        return []

    def trigger_pipeline(self, *, project, ref, variables=None):
        return {}

    def quality_check(self, *, project, ref):
        return {}


def setup_function() -> None:
    reset_backend_caches()


def teardown_function() -> None:
    reset_backend_caches()


def test_load_backend_returns_none_when_setting_empty() -> None:
    assert _load_backend("TEATREE_NONEXISTENT_SETTING") is None


@override_settings(TEATREE_CODE_HOST="tests.teatree_backends.test_loader._DummyCodeHost")
def test_load_backend_imports_and_instantiates() -> None:
    backend = _load_backend("TEATREE_CODE_HOST")
    assert backend is not None
    assert type(backend).__name__ == "_DummyCodeHost"


@override_settings(TEATREE_CODE_HOST="tests.teatree_backends.test_loader._DummyCodeHost")
def test_get_code_host_returns_instance() -> None:
    result = get_code_host()
    assert result is not None
    assert type(result).__name__ == "_DummyCodeHost"


def test_get_code_host_returns_none_when_not_configured() -> None:
    result = get_code_host()
    assert result is None


@override_settings(TEATREE_ISSUE_TRACKER="tests.teatree_backends.test_loader._DummyIssueTracker")
def test_get_issue_tracker_returns_instance() -> None:
    result = get_issue_tracker()
    assert result is not None
    assert type(result).__name__ == "_DummyIssueTracker"


def test_get_issue_tracker_returns_none_when_not_configured() -> None:
    result = get_issue_tracker()
    assert result is None


@override_settings(TEATREE_CHAT_NOTIFIER="tests.teatree_backends.test_loader._DummyChatNotifier")
def test_get_chat_notifier_returns_instance() -> None:
    result = get_chat_notifier()
    assert result is not None
    assert type(result).__name__ == "_DummyChatNotifier"


def test_get_chat_notifier_returns_none_when_not_configured() -> None:
    result = get_chat_notifier()
    assert result is None


@override_settings(TEATREE_ERROR_TRACKER="tests.teatree_backends.test_loader._DummyErrorTracker")
def test_get_error_tracker_returns_instance() -> None:
    result = get_error_tracker()
    assert result is not None
    assert type(result).__name__ == "_DummyErrorTracker"


def test_get_error_tracker_returns_none_when_not_configured() -> None:
    result = get_error_tracker()
    assert result is None


@override_settings(TEATREE_CI_SERVICE="tests.teatree_backends.test_loader._DummyCIService")
def test_get_ci_service_returns_explicit_backend() -> None:
    result = get_ci_service()
    assert result is not None
    assert type(result).__name__ == "_DummyCIService"


def test_get_ci_service_returns_none_when_no_config_or_token() -> None:
    result = get_ci_service()
    assert result is None


@override_settings(TEATREE_GITLAB_TOKEN="gl-test-token")
def test_get_ci_service_auto_creates_gitlab_ci_when_token_present() -> None:
    from teatree.backends.gitlab_ci import GitLabCIService  # noqa: PLC0415

    result = get_ci_service()
    assert isinstance(result, GitLabCIService)


@override_settings(TEATREE_GITLAB_TOKEN="gl-test-token")
def test_get_code_host_auto_creates_gitlab_when_token_present() -> None:
    """get_code_host auto-configures GitLabCodeHost when TEATREE_GITLAB_TOKEN is set (lines 40-42)."""
    from teatree.backends.gitlab import GitLabCodeHost  # noqa: PLC0415

    result = get_code_host()
    assert isinstance(result, GitLabCodeHost)


def test_reset_backend_caches_clears_all() -> None:
    # Just verify it runs without error and allows fresh lookups
    reset_backend_caches()
    assert get_code_host() is None
    assert get_issue_tracker() is None
    assert get_chat_notifier() is None
    assert get_error_tracker() is None
    assert get_ci_service() is None
