"""Backend loader — builds code host / CI from explicit credentials.

The functions here do NOT import from ``teatree.core``; callers are
responsible for resolving overlay config and passing tokens / URLs.
"""

from functools import lru_cache

from teatree.backends.protocols import ChatNotifier, CIService, CodeHost, ErrorTracker, IssueTracker


@lru_cache(maxsize=1)
def get_code_host(
    *,
    github_token: str = "",
    gitlab_token: str = "",
    gitlab_url: str = "",
) -> CodeHost | None:
    """Return a configured code-host backend, or ``None``.

    Callers should resolve tokens from the overlay and pass them explicitly.
    """
    if github_token:
        from teatree.backends.github import GitHubCodeHost  # noqa: PLC0415

        return GitHubCodeHost(token=github_token)

    if gitlab_token:
        from teatree.backends.gitlab import GitLabCodeHost  # noqa: PLC0415

        return GitLabCodeHost(token=gitlab_token, base_url=gitlab_url)
    return None


@lru_cache(maxsize=1)
def get_issue_tracker() -> IssueTracker | None:
    return None


@lru_cache(maxsize=1)
def get_chat_notifier() -> ChatNotifier | None:
    return None


@lru_cache(maxsize=1)
def get_error_tracker() -> ErrorTracker | None:
    return None


@lru_cache(maxsize=1)
def get_ci_service(
    *,
    gitlab_token: str = "",
    gitlab_url: str = "",
) -> CIService | None:
    """Return a configured CI-service backend, or ``None``.

    Callers should resolve tokens from the overlay and pass them explicitly.
    """
    if gitlab_token:
        from teatree.backends.gitlab_api import GitLabAPI  # noqa: PLC0415
        from teatree.backends.gitlab_ci import GitLabCIService  # noqa: PLC0415

        return GitLabCIService(client=GitLabAPI(token=gitlab_token, base_url=gitlab_url or "https://gitlab.com/api/v4"))
    return None


def reset_backend_caches() -> None:
    get_code_host.cache_clear()
    get_issue_tracker.cache_clear()
    get_chat_notifier.cache_clear()
    get_error_tracker.cache_clear()
    get_ci_service.cache_clear()
