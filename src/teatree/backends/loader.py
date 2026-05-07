"""Backend loader — selects code-host and messaging implementations per overlay.

The loader is the only place that branches on platform. Caller code consumes
:class:`teatree.backends.protocols.CodeHostBackend` and
:class:`teatree.backends.protocols.MessagingBackend` uniformly; the choice of
GitHub vs GitLab and Slack vs Noop is encoded on ``OverlayBase.config``.
"""

from functools import lru_cache
from typing import TYPE_CHECKING

from teatree.backends.github import GitHubCodeHost
from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab_api import GitLabAPI
from teatree.backends.gitlab_ci import GitLabCIService
from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.backends.protocols import CIService, CodeHostBackend, MessagingBackend
from teatree.backends.slack_bot import SlackBotBackend
from teatree.utils.secrets import read_pass

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


def get_code_host(overlay: "OverlayBase") -> CodeHostBackend | None:
    """Return the configured CodeHostBackend for *overlay*, or ``None``.

    Selection follows ``overlay.config.code_host``; falls back to inspecting
    the available tokens when the field is unset (legacy behaviour kept so
    older overlays that haven't migrated still work).
    """
    choice = getattr(overlay.config, "code_host", "")
    github_token = overlay.config.get_github_token()
    gitlab_token = overlay.config.get_gitlab_token()

    if choice == "github" or (not choice and github_token):
        return GitHubCodeHost(token=github_token) if github_token else None

    if choice == "gitlab" or (not choice and gitlab_token):
        return GitLabCodeHost(token=gitlab_token, base_url=overlay.config.gitlab_url) if gitlab_token else None

    if choice in {"", "github", "gitlab"}:
        return None
    msg = f"Unknown code_host: {choice!r}"
    raise ValueError(msg)


def get_messaging(overlay: "OverlayBase") -> MessagingBackend:
    """Return the configured MessagingBackend for *overlay*.

    Default is :class:`NoopMessagingBackend` so callers always get a
    Protocol-conforming object — no per-call ``is None`` guards.
    """
    choice = getattr(overlay.config, "messaging_backend", "") or "noop"
    if choice == "slack":
        token_ref = getattr(overlay.config, "slack_bot_token_ref", "")
        return SlackBotBackend(
            bot_token=read_pass(f"{token_ref}-bot") if token_ref else overlay.config.get_slack_token(),
            app_token=read_pass(f"{token_ref}-app") if token_ref else "",
            user_id=getattr(overlay.config, "slack_user_id", ""),
        )
    if choice == "noop":
        return NoopMessagingBackend()
    msg = f"Unknown messaging_backend: {choice!r}"
    raise ValueError(msg)


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
        return GitLabCIService(client=GitLabAPI(token=gitlab_token, base_url=gitlab_url or "https://gitlab.com/api/v4"))
    return None


def reset_backend_caches() -> None:
    get_ci_service.cache_clear()
