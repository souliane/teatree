"""Overlay-bound GitLab HTTP client for loop scanner reads."""

from teatree.backends.gitlab.api import GitLabAPI
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState


def overlay_gitlab_base_url(overlay_name: str) -> str:
    """Return only the named overlay's API URL; credentials use the central resolver."""
    try:
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: loaded at tick time, not import
    except Exception:  # noqa: BLE001 — overlay loader unavailable degrades to the legacy default
        return "https://gitlab.com/api/v4"
    try:
        overlay = get_overlay(overlay_name or None)
    except Exception:  # noqa: BLE001 — TOML-only overlays use the raw URL below
        return _overlay_gitlab_base_url_from_toml(overlay_name)
    base_url = getattr(overlay.config, "gitlab_url", "https://gitlab.com/api/v4")
    return base_url or "https://gitlab.com/api/v4"


def _overlay_gitlab_base_url_from_toml(overlay_name: str) -> str:
    """Resolve a TOML-only overlay's API URL without touching credentials."""
    try:
        from teatree.config import load_config  # noqa: PLC0415 — deferred: loaded at tick time, not import
    except Exception:  # noqa: BLE001 — unavailable config uses the public default
        return "https://gitlab.com/api/v4"
    overlays = load_config().raw.get("overlays") or {}
    cfg = overlays.get(overlay_name)
    if not isinstance(cfg, dict):
        return "https://gitlab.com/api/v4"
    base_url = str(cfg.get("gitlab_url", "https://gitlab.com")).rstrip("/")
    if not base_url.endswith("/api/v4"):
        base_url = f"{base_url}/api/v4"
    return base_url


class MissingGitLabTokenError(ValueError):
    pass


def gitlab_api_for(resolution: ForgeTokenResolution) -> GitLabAPI:
    if resolution.state is not ForgeTokenState.TOKEN or not resolution.token:
        raise MissingGitLabTokenError
    return GitLabAPI(token=resolution.token, base_url=overlay_gitlab_base_url(resolution.overlay_name))
