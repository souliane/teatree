"""Settings-driven backend loader.

Each backend concern (code host, issue tracker, chat, errors) is configured
via a Django setting that points to an import path for the implementation class.
"""

from functools import lru_cache

from django.conf import settings
from django.utils.module_loading import import_string

from teetree.backends.protocols import ChatNotifier, CIService, CodeHost, ErrorTracker, IssueTracker

_BACKENDS: dict[str, tuple[str, type]] = {
    "TEATREE_CODE_HOST": ("code host", CodeHost),
    "TEATREE_ISSUE_TRACKER": ("issue tracker", IssueTracker),
    "TEATREE_CHAT_NOTIFIER": ("chat notifier", ChatNotifier),
    "TEATREE_ERROR_TRACKER": ("error tracker", ErrorTracker),
    "TEATREE_CI_SERVICE": ("CI service", CIService),
}


def _load_backend(setting_name: str) -> object | None:
    path = getattr(settings, setting_name, "")
    if not path:
        return None
    return import_string(path)()


@lru_cache(maxsize=1)
def get_code_host() -> CodeHost | None:
    backend = _load_backend("TEATREE_CODE_HOST")
    if backend is not None:
        return backend  # type: ignore[return-value]

    from django.conf import settings as django_settings  # noqa: PLC0415

    token = getattr(django_settings, "TEATREE_GITLAB_TOKEN", "")
    if token:
        from teetree.backends.gitlab import GitLabCodeHost  # noqa: PLC0415

        return GitLabCodeHost()
    return None


@lru_cache(maxsize=1)
def get_issue_tracker() -> IssueTracker | None:
    return _load_backend("TEATREE_ISSUE_TRACKER")  # type: ignore[return-value]


@lru_cache(maxsize=1)
def get_chat_notifier() -> ChatNotifier | None:
    return _load_backend("TEATREE_CHAT_NOTIFIER")  # type: ignore[return-value]


@lru_cache(maxsize=1)
def get_error_tracker() -> ErrorTracker | None:
    return _load_backend("TEATREE_ERROR_TRACKER")  # type: ignore[return-value]


@lru_cache(maxsize=1)
def get_ci_service() -> CIService | None:
    backend = _load_backend("TEATREE_CI_SERVICE")
    if backend is not None:
        return backend  # type: ignore[return-value]
    # Auto-configure from GitLab settings if no explicit CI service
    from django.conf import settings as django_settings  # noqa: PLC0415

    token = getattr(django_settings, "TEATREE_GITLAB_TOKEN", "")
    if token:
        from teetree.backends.gitlab_ci import GitLabCIService  # noqa: PLC0415

        return GitLabCIService()
    return None


def reset_backend_caches() -> None:
    get_code_host.cache_clear()
    get_issue_tracker.cache_clear()
    get_chat_notifier.cache_clear()
    get_error_tracker.cache_clear()
    get_ci_service.cache_clear()
