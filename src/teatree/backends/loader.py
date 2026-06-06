"""Backend loader — selects code-host and messaging implementations per overlay.

The loader is the only place that branches on platform. Caller code consumes
:class:`teatree.core.backend_protocols.CodeHostBackend` and
:class:`teatree.core.backend_protocols.MessagingBackend` uniformly; the choice of
GitHub vs GitLab and Slack vs Noop is encoded on ``OverlayBase.config``.
"""

from functools import lru_cache
from typing import TYPE_CHECKING

from teatree.backends.github import GitHubCodeHost
from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab_api import GitLabAPI
from teatree.backends.gitlab_ci import GitLabCIService
from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.backends.slack_bot import SlackBotBackend
from teatree.core.backend_protocols import CIService, CodeHostBackend, MessagingBackend
from teatree.utils.secrets import read_pass

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


def get_code_host(overlay: "OverlayBase") -> CodeHostBackend | None:
    """Return the configured CodeHostBackend for *overlay*, or ``None``.

    Selection follows ``overlay.config.code_host``; falls back to inspecting
    the available tokens when the field is unset.

    Pre-#976 single-platform callers — anything that wires a single host
    into a Django view or CLI command — keep calling this. The multi-host
    loop scanner stack calls :func:`get_code_hosts` instead.
    """
    choice = overlay.config.code_host
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


def get_code_hosts(overlay: "OverlayBase") -> list[CodeHostBackend]:
    """Return every CodeHostBackend an overlay opts into (#976).

    A user with both GitHub and GitLab PATs configured on the same overlay
    expects the loop to scan both forges. The legacy :func:`get_code_host`
    silently dropped one because it returned the first match — single-host
    callers keep using it; the loop scanner stack uses this one so both
    platforms surface PRs/issues/reviews.

    ``code_host`` choice is honoured as a hard constraint when set: a user
    who explicitly pins one platform gets only that platform, even if the
    other token resolves. Empty / auto picks both whenever tokens resolve.
    """
    choice = overlay.config.code_host
    hosts: list[CodeHostBackend] = []
    github_token = overlay.config.get_github_token()
    gitlab_token = overlay.config.get_gitlab_token()

    if choice == "github":
        if github_token:
            hosts.append(GitHubCodeHost(token=github_token))
        return hosts
    if choice == "gitlab":
        if gitlab_token:
            hosts.append(GitLabCodeHost(token=gitlab_token, base_url=overlay.config.gitlab_url))
        return hosts
    if choice not in {"", "github", "gitlab"}:
        msg = f"Unknown code_host: {choice!r}"
        raise ValueError(msg)

    # Auto mode: build one host per token that resolves. GitHub first so
    # ``OverlayBackends.host`` (= ``hosts[0]``) preserves the legacy
    # GitHub-wins-when-both-set precedence that single-platform callers
    # downstream depend on.
    if github_token:
        hosts.append(GitHubCodeHost(token=github_token))
    if gitlab_token:
        hosts.append(GitLabCodeHost(token=gitlab_token, base_url=overlay.config.gitlab_url))
    return hosts


def get_code_host_for_url(overlay: "OverlayBase", issue_url: str) -> CodeHostBackend | None:
    """Return the code host matching *issue_url*'s domain, using *overlay*'s tokens.

    Unlike :func:`get_code_host` (which picks one platform per overlay),
    this resolves per-URL — essential when an overlay's tickets span both
    GitHub and GitLab.
    """
    if "github.com" in issue_url:
        token = overlay.config.get_github_token()
        return GitHubCodeHost(token=token) if token else None
    if "gitlab" in issue_url:
        token = overlay.config.get_gitlab_token()
        return GitLabCodeHost(token=token, base_url=overlay.config.gitlab_url) if token else None
    return get_code_host(overlay)


def get_messaging(overlay: "OverlayBase") -> MessagingBackend:
    """Return the configured MessagingBackend for *overlay*.

    Default is :class:`NoopMessagingBackend` so callers always get a
    Protocol-conforming object — no per-call ``is None`` guards.

    The optional ``user_token_ref`` field on ``OverlayConfig`` points at a
    ``pass`` entry holding the human user's OAuth token (``xoxp-…``).  When
    set, ``SlackBotBackend`` authenticates reactions through that token so
    Slack-Connect externally-shared channels accept them — the bot token is
    rejected there by the workspace restriction policy.

    This is a loop construction path, so a malformed user token degrades to
    bot-only (``degrade_bad_user_token=True``) instead of raising: a
    Slack-only credential typo must never wedge merges, CI, or PR sweeps.
    """
    choice = overlay.config.messaging_backend or "noop"
    if choice == "slack":
        token_ref = overlay.config.slack_token_ref
        user_token_ref = getattr(overlay.config, "user_token_ref", "")
        return SlackBotBackend(
            bot_token=read_pass(f"{token_ref}-bot") if token_ref else overlay.config.get_slack_token(),
            app_token=read_pass(f"{token_ref}-app") if token_ref else "",
            user_token=read_pass(user_token_ref) if user_token_ref else "",
            user_id=overlay.config.slack_user_id,
            # Setup-time provisioned IM channel id (#1342) — see
            # ``OverlayConfig.slack_dm_channel_id``. ``getattr`` keeps older
            # third-party overlay subclasses (that pre-date the field)
            # working without an explicit default.
            dm_channel_id=getattr(overlay.config, "slack_dm_channel_id", ""),
            degrade_bad_user_token=True,
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
