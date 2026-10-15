"""Overlay-bound GitLab HTTP client for loop scanner reads."""

from django.core.exceptions import ImproperlyConfigured

import teatree.core.overlay_loader as _overlay_loader
from teatree.backends.gitlab.api import GitLabAPI
from teatree.config import load_config
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState

_DEFAULT_API_URL = "https://gitlab.com/api/v4"


def overlay_gitlab_base_url(overlay_name: str) -> str:
    """Return only the named overlay's API URL; credentials use the central resolver."""
    try:
        overlay = _overlay_loader.get_overlay(overlay_name or None)
    except ImproperlyConfigured:
        return _overlay_gitlab_base_url_from_toml(overlay_name)
    return overlay.config.gitlab_url or _DEFAULT_API_URL


def _overlay_gitlab_base_url_from_toml(overlay_name: str) -> str:
    """Resolve a TOML-only overlay's API URL without touching credentials."""
    overlays = load_config().raw.get("overlays") or {}
    cfg = overlays.get(overlay_name)
    if not isinstance(cfg, dict):
        return _DEFAULT_API_URL
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
