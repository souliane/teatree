"""Backend loader — auto-configures code host, CI, chat from overlay config."""

from functools import lru_cache

from teatree.backends.protocols import ChatNotifier, CIService, CodeHost, ErrorTracker, IssueTracker


@lru_cache(maxsize=1)
def get_code_host() -> CodeHost | None:
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    try:
        overlay = get_overlay()
    except Exception:  # noqa: BLE001
        return None

    if overlay.config.get_gitlab_token():
        from teatree.backends.gitlab import GitLabCodeHost  # noqa: PLC0415

        return GitLabCodeHost()
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
def get_ci_service() -> CIService | None:
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    try:
        overlay = get_overlay()
    except Exception:  # noqa: BLE001
        return None

    if overlay.config.get_gitlab_token():
        from teatree.backends.gitlab_ci import GitLabCIService  # noqa: PLC0415

        return GitLabCIService()
    return None


def reset_backend_caches() -> None:
    get_code_host.cache_clear()
    get_issue_tracker.cache_clear()
    get_chat_notifier.cache_clear()
    get_error_tracker.cache_clear()
    get_ci_service.cache_clear()
