"""Overlay-aware backend factory — resolves config and builds backends.

This module bridges ``teatree.core`` (overlay registry) and
``teatree.backends`` (loader) so that callers in ``core`` and ``cli`` don't
need to extract tokens or branch on platform themselves.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from teatree.backends.loader import get_ci_service, get_code_host, get_messaging
from teatree.backends.loader import reset_backend_caches as _reset_loader_caches
from teatree.backends.protocols import CIService, CodeHostBackend, MessagingBackend
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_all_overlays, get_overlay
from teatree.paths import find_overlay_db


@dataclass(frozen=True, slots=True)
class OverlayBackends:
    """Backends and config slice for one registered overlay.

    The loop tick builds one set of scanners per ``OverlayBackends`` so a
    user with multiple overlays (e.g. one per GitHub identity) sees PRs,
    issues, and Slack mentions from all of them in one statusline.
    """

    name: str
    host: CodeHostBackend | None
    messaging: MessagingBackend | None
    ready_labels: tuple[str, ...]
    exclude_labels: tuple[str, ...] = ()
    overlay: OverlayBase | None = None
    auto_start_assigned_issues: bool = False
    max_concurrent_auto_starts: int = 1
    external_db: Path | None = None


@lru_cache(maxsize=1)
def code_host_from_overlay() -> CodeHostBackend | None:
    """Build a code-host backend using the active overlay's credentials.

    Cached for the loop tick — every scanner that needs the host shares one
    instance per process. Tests that swap overlays must call
    :func:`reset_backend_caches` to discard the cached client.
    """
    try:
        overlay = get_overlay()
    except ImproperlyConfigured:
        return None
    return get_code_host(overlay)


@lru_cache(maxsize=1)
def messaging_from_overlay() -> MessagingBackend | None:
    """Build a messaging backend using the active overlay's config (cached)."""
    try:
        overlay = get_overlay()
    except ImproperlyConfigured:
        return None
    return get_messaging(overlay)


def ci_service_from_overlay() -> CIService | None:
    """Build a CI-service backend using the active overlay's credentials."""
    try:
        overlay = get_overlay()
    except ImproperlyConfigured:
        return None

    return get_ci_service(
        gitlab_token=overlay.config.get_gitlab_token(),
        gitlab_url=overlay.config.gitlab_url,
    )


def iter_overlay_backends() -> list[OverlayBackends]:
    """Build :class:`OverlayBackends` for every registered overlay.

    Overlays whose credentials don't resolve get ``host=None`` /
    ``messaging=None`` — the caller decides whether to skip them.

    Also includes TOML-configured overlays that have credentials but no
    Python class (project-directory-only overlays reached via subprocess).
    """
    out: list[OverlayBackends] = []
    found_names: set[str] = set()

    for name, overlay in get_all_overlays().items():
        found_names.add(name)
        try:
            host = get_code_host(overlay)
        except (ImproperlyConfigured, ValueError):
            host = None
        try:
            messaging = get_messaging(overlay)
        except (ImproperlyConfigured, ValueError):
            messaging = None
        out.append(
            OverlayBackends(
                name=name,
                host=host,
                messaging=messaging,
                ready_labels=tuple(overlay.config.ready_labels),
                exclude_labels=tuple(overlay.config.exclude_labels),
                overlay=overlay,
                auto_start_assigned_issues=bool(overlay.config.auto_start_assigned_issues),
                max_concurrent_auto_starts=int(overlay.config.max_concurrent_auto_starts),
            ),
        )

    out.extend(_backends_from_toml(found_names))
    return out


def _backends_from_toml(already_found: set[str]) -> list[OverlayBackends]:
    """Build backends for TOML overlays not discovered via entry points."""
    from teatree.config import load_config  # noqa: PLC0415

    result: list[OverlayBackends] = []
    config = load_config()
    for name, overlay_cfg in (config.raw.get("overlays") or {}).items():
        if name in already_found or not isinstance(overlay_cfg, dict):
            continue
        host = _host_from_toml(overlay_cfg)
        messaging = _messaging_from_toml(overlay_cfg)
        db_path = _find_external_db(name, overlay_cfg)
        if host is None and messaging is None and db_path is None:
            continue
        result.append(
            OverlayBackends(
                name=name,
                host=host,
                messaging=messaging,
                ready_labels=tuple(overlay_cfg.get("ready_labels", ())),
                exclude_labels=tuple(overlay_cfg.get("exclude_labels", ())),
                external_db=db_path,
            ),
        )
    return result


def _find_external_db(name: str, cfg: dict) -> Path | None:
    project_path = cfg.get("path", "")
    if not project_path:
        return None
    return find_overlay_db(name, project_path)


def _host_from_toml(cfg: dict) -> CodeHostBackend | None:
    from teatree.utils.secrets import read_pass  # noqa: PLC0415

    gitlab_token_ref = cfg.get("gitlab_token_ref", "")
    github_token_ref = cfg.get("github_token_ref", "")
    gitlab_url = cfg.get("gitlab_url", "https://gitlab.com")

    if gitlab_token_ref:
        token = read_pass(gitlab_token_ref)
        if token:
            from teatree.backends.gitlab import GitLabCodeHost  # noqa: PLC0415

            return GitLabCodeHost(token=token, base_url=gitlab_url)
    if github_token_ref:
        token = read_pass(github_token_ref)
        if token:
            from teatree.backends.github import GitHubCodeHost  # noqa: PLC0415

            return GitHubCodeHost(token=token)
    return None


def _messaging_from_toml(cfg: dict) -> MessagingBackend | None:
    if cfg.get("messaging_backend") != "slack":
        return None
    from teatree.backends.slack_bot import SlackBotBackend  # noqa: PLC0415
    from teatree.utils.secrets import read_pass  # noqa: PLC0415

    token_ref = cfg.get("slack_token_ref", "")
    if not token_ref:
        return None
    bot_token = read_pass(f"{token_ref}-bot")
    app_token = read_pass(f"{token_ref}-app")
    user_id = cfg.get("slack_user_id", "")
    if bot_token:
        return SlackBotBackend(bot_token=bot_token, app_token=app_token or "", user_id=user_id)
    return None


def reset_backend_caches() -> None:
    """Clear all per-overlay backend caches.

    Call when the active overlay changes (overlay reload, multi-overlay
    test fixtures) so the next factory call rebuilds with fresh credentials.
    """
    code_host_from_overlay.cache_clear()
    messaging_from_overlay.cache_clear()
    _reset_loader_caches()


__all__ = [
    "OverlayBackends",
    "ci_service_from_overlay",
    "code_host_from_overlay",
    "get_ci_service",
    "get_code_host",
    "get_messaging",
    "iter_overlay_backends",
    "messaging_from_overlay",
    "reset_backend_caches",
]
