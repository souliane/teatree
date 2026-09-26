"""Backend loader — selects code-host and messaging implementations per overlay.

The loader is the only place that branches on platform. Caller code consumes
:class:`teatree.core.backend_protocols.CodeHostBackend` and
:class:`teatree.core.backend_protocols.MessagingBackend` uniformly; the choice of
GitHub vs GitLab and Slack vs Noop is encoded on ``OverlayBase.config``.
"""

import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from teatree.backends.github import GitHubCodeHost
from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab.api import GitLabAPI
from teatree.backends.gitlab.ci import GitLabCIService
from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.backend_protocols import (
    BackendResolutionError,
    CIService,
    CodeHostBackend,
    MessagingBackend,
    PrOpenState,
)
from teatree.core.backend_registry import parse_slack_scope_profile
from teatree.core.messaging_tokens import diagnose_configured_ref, resolve_messaging_tokens
from teatree.forge_credentials import ForgeTokenState, resolve_overlay_token, resolve_repo_token, resolve_url_token
from teatree.utils import git
from teatree.utils.forge import forge_from_remote

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)


def _github_host(overlay: "OverlayBase") -> GitHubCodeHost | None:
    """Return a GitHub host only when the overlay's routed token resolves."""
    resolution = resolve_overlay_token(overlay, credential="github_token")
    return GitHubCodeHost(token=resolution.token) if resolution.state is ForgeTokenState.TOKEN else None


def get_code_host(overlay: "OverlayBase") -> CodeHostBackend | None:
    """Return the configured CodeHostBackend for *overlay*, or ``None``.

    Selection follows ``overlay.config.code_host`` and falls back to inspecting
    the routed tokens when the field is unset. A tokenless GitHub route stays
    unconfigured; ambient ``gh`` authentication is never inherited.

    Pre-#976 single-platform callers — anything that wires a single host
    into a Django view or CLI command — keep calling this. The multi-host
    loop scanner stack calls :func:`get_code_hosts` instead.
    """
    choice = overlay.config.code_host
    if choice not in {"", "github", "gitlab"}:
        msg = f"Unknown code_host: {choice!r}"
        raise ValueError(msg)

    if choice == "github":
        return _github_host(overlay)

    gitlab_token = overlay.config.get_gitlab_token()
    if choice == "gitlab":
        return GitLabCodeHost(token=gitlab_token, base_url=overlay.config.gitlab_url) if gitlab_token else None

    github_host = _github_host(overlay)
    if github_host is not None:
        return github_host
    if gitlab_token:
        return GitLabCodeHost(token=gitlab_token, base_url=overlay.config.gitlab_url)
    return None


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
    if choice not in {"", "github", "gitlab"}:
        msg = f"Unknown code_host: {choice!r}"
        raise ValueError(msg)

    hosts: list[CodeHostBackend] = []
    gitlab_token = overlay.config.get_gitlab_token()

    if choice == "github":
        github_host = _github_host(overlay)
        if github_host is not None:
            hosts.append(github_host)
        return hosts
    if choice == "gitlab":
        if gitlab_token:
            hosts.append(GitLabCodeHost(token=gitlab_token, base_url=overlay.config.gitlab_url))
        return hosts

    github_host = _github_host(overlay)
    if github_host is not None:
        hosts.append(github_host)
    if gitlab_token:
        hosts.append(GitLabCodeHost(token=gitlab_token, base_url=overlay.config.gitlab_url))
    return hosts


def _host_backend(
    overlay: "OverlayBase",
    forge: Literal["github", "gitlab"],
    remote: str,
) -> CodeHostBackend | None:
    """Build the backend for a resolved *forge*, or ``None`` when unauthenticated.

    *remote* scopes the credential to the repo being acted
    on rather than answering with one token everywhere — the AUTHORING credential
    specifically, which is the one the forge then bars from approving. The review/
    approve surface deliberately reads the overlay-wide OWNER credential instead
    (:func:`teatree.cli.review.forge_target.read_token`).

    The scoping is resolved from the REPO across every registered overlay
    (:func:`teatree.core.authoring_credential.gitlab_token_for_remote`), not from
    *overlay* alone: read off the ambient overlay, a repo answered a different
    identity per ``t3 <overlay>`` prefix, so a fixed-prefix entrypoint opened MRs
    under the owner — who is then barred from approving them.
    """
    from teatree.core.authoring_credential import (  # noqa: PLC0415 — deferred: backends <-> core cycle
        gitlab_token_for_remote,
    )

    if forge == "github":
        resolution = resolve_url_token(remote, credential="github_token")
        return GitHubCodeHost(token=resolution.token) if resolution.state is ForgeTokenState.TOKEN else None
    token = gitlab_token_for_remote(overlay.config, remote)
    return GitLabCodeHost(token=token, base_url=overlay.config.gitlab_url) if token else None


def get_code_host_for_url(overlay: "OverlayBase", issue_url: str) -> CodeHostBackend | None:
    """Return the code host matching *issue_url*'s domain, using *overlay*'s tokens.

    Unlike :func:`get_code_host` (which picks one platform per overlay),
    this resolves per-URL — essential when an overlay's tickets span both
    GitHub and GitLab.
    """
    forge = forge_from_remote(issue_url)
    if not forge:
        return get_code_host(overlay)
    return _host_backend(overlay, forge, issue_url)


def pr_open_state(pr_url: str) -> PrOpenState:
    """Live OPEN / MERGED / CLOSED state of the PR/MR at *pr_url*, per the forge.

    Resolves the per-URL code host with the owning overlay's credentials and reads
    :meth:`CodeHostBackend.get_pr_open_state`. Every indeterminate case — an empty
    URL, an unresolvable overlay or host, any transport error — collapses to
    :attr:`PrOpenState.UNKNOWN`, so a caller can never mistake a failed read for a
    definite verdict. Distinguishing MERGED from CLOSED is what lets the board
    reconcile advance a landed ticket while resolving an abandoned one.
    """
    if not pr_url:
        return PrOpenState.UNKNOWN
    from teatree.core.overlay_loader import get_overlay_for_url  # noqa: PLC0415 — deferred: backends ↔ core cycle

    try:
        host = get_code_host_for_url(get_overlay_for_url(pr_url), pr_url)
        if host is None:
            return PrOpenState.UNKNOWN
        return host.get_pr_open_state(pr_url=pr_url)
    except Exception:
        logger.exception("Live-state check failed for %s", pr_url)
        return PrOpenState.UNKNOWN


def pr_is_merged_or_closed(pr_url: str) -> bool:
    """Whether the PR/MR at *pr_url* is provably MERGED or CLOSED (#2081).

    Fail-OPEN: only a *definite* MERGED/CLOSED returns ``True``; the UNKNOWN that
    :func:`pr_open_state` collapses every failure into returns ``False`` so a
    transient API hiccup never suppresses a downstream action.
    """
    return pr_open_state(pr_url) in {PrOpenState.MERGED, PrOpenState.CLOSED}


def get_code_host_for_repo(overlay: "OverlayBase", repo_path: str) -> CodeHostBackend | None:
    """Return the code host matching *repo_path*'s actual origin remote host.

    The forge is derived from where the repo physically lives — the
    ``origin`` remote URL — not from token-presence precedence. An overlay
    carrying both a GitHub and a GitLab PAT must still open the PR on the
    repo's own forge; resolving by token order picked GitHub for a
    GitLab-hosted repo and ran ``gh`` against a GitLab remote (#2025).

    Raises :class:`BackendResolutionError` when the origin host is a
    recognised forge but the overlay has no working credentials for it —
    surfacing the mismatch BEFORE the PR-creation attempt instead of letting
    a raw ``gh``/``glab`` GraphQL error be the first signal. Falls back to
    :func:`get_code_host` (the overlay default) only when the repo has no
    origin remote / an unrecognised host.

    GitHub and GitLab both require the credential route owned by the repo.
    """
    remote = git.remote_url(repo=repo_path)
    forge = forge_from_remote(remote) if remote else ""
    if not forge:
        return get_code_host(overlay)
    if forge == "github":
        return _github_host_for_repo(repo_path, remote)
    backend = _host_backend(overlay, forge, remote)
    if backend is not None:
        return backend
    msg = (
        f"repo origin resolves to the {forge} forge ({remote!r}) but the active "
        f"overlay has no {forge} credentials configured — cannot open a PR. "
        f"Configure a {forge} token for this overlay."
    )
    raise BackendResolutionError(msg)


def _github_host_for_repo(repo_path: str, remote: str) -> CodeHostBackend:
    """Return the owning overlay's routed GitHub host for *repo_path*."""
    resolution = resolve_repo_token(repo_path, credential="github_token")
    if resolution.state is ForgeTokenState.TOKEN:
        return GitHubCodeHost(token=resolution.token)
    msg = (
        f"repo origin resolves to the github forge ({remote!r}) but the active "
        f"overlay has no usable github credential ({resolution.state.value}: {resolution.detail}) — "
        "cannot open a PR. Configure github_token_pass_key for the owning overlay."
    )
    raise BackendResolutionError(msg)


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
    Slack-only credential typo must never wedge merges, CI, or PR sweeps. An
    EMPTY bot token degrades the whole backend to noop for the same reason and
    one more: ``SlackBotBackend`` short-circuits every post to ``{}`` before any
    HTTP when it has no token, and the notify layer reads that empty body as a
    Slack refusal — reporting ``conversations.open ok:false`` and "no message ts"
    for a credential fault that never reached Slack. Noop is the truthful
    degradation, and it lets ``resolve_owner_dm_backend`` park the notification
    as recoverable instead of burning it on a fabricated API error.
    """
    choice = overlay.config.messaging_backend or "noop"
    if choice == "slack":
        tokens = resolve_messaging_tokens(
            slack_token_ref=overlay.config.slack_token_ref,
            user_token_ref=overlay.config.user_token_ref,
            bot_fallback=overlay.config.get_slack_token(),
        )
        if not tokens.bot:
            logger.warning(
                "messaging backend 'slack' resolved an EMPTY bot token (%s) — degrading to noop. %s",
                diagnose_configured_ref("slack_token_ref", overlay.config.slack_token_ref, suffix="-bot")
                or "no slack_token_ref configured; the overlay's get_slack_token() returned empty",
                "A DM cannot be delivered until the credential store resolves again.",
            )
            return NoopMessagingBackend()
        return SlackBotBackend(
            bot_token=tokens.bot,
            app_token=tokens.app,
            user_token=tokens.user,
            user_id=overlay.config.slack_user_id,
            # Setup-time provisioned IM channel id (#1342) — see
            # ``OverlayConfig.slack_dm_channel_id``.
            dm_channel_id=overlay.config.slack_dm_channel_id,
            degrade_bad_user_token=True,
            # dm_only scope profile: the backend refuses every outbound but the
            # owner's own DM (``assert_owner_dm`` at its token funnels).
            owner_dm_only=parse_slack_scope_profile(overlay.config.slack_scope_profile) == "dm_only",
        )
    if choice == "noop":
        return NoopMessagingBackend()
    msg = f"Unknown messaging_backend: {choice!r}"
    raise ValueError(msg)


# Bounded so multiple overlays (each keyed on its own token/url) coexist —
# ``maxsize=1`` evicted the previous overlay's service on every alternating
# resolve, rebuilding the GitLabAPI client each time. Realistic setups run a
# handful of overlays, so a small ceiling avoids the thrash without unbounded
# growth on token rotation.
@lru_cache(maxsize=8)
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
