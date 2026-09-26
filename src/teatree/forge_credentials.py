"""Import-safe forge credential resolution seam.

Forge consumers live at several architecture layers, while the owning-overlay
lookup and DB-backed pass-key route live in :mod:`teatree.core`.  This module
keeps the dependency pointing down: consumers call one state-preserving seam,
and the overlay loader registers the core-side provider once it is available.
Before registration the seam fails closed; it never consults ambient forge
credentials.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ForgeTokenState(StrEnum):
    TOKEN = "token"  # noqa: S105 - state name, not a credential
    UNSET = "unset"
    UNREADABLE = "unreadable"


class ForgeCredentialTarget(StrEnum):
    OVERLAY = "overlay"
    NAMED_OVERLAY = "named-overlay"
    REPO = "repo"
    URL = "url"
    SLUG = "slug"


@dataclass(frozen=True, slots=True)
class ForgeTokenResolution:
    credential: str
    overlay_name: str
    state: ForgeTokenState
    token: str = ""
    pass_key: str = ""
    route_source: str = "unset"
    detail: str = ""

    @property
    def setting(self) -> str:
        return f"{self.credential}_pass_key"


@dataclass(frozen=True, slots=True)
class ForgeCredentialRequest:
    target_kind: ForgeCredentialTarget
    target: Any
    credential: str
    overlay_name: str = ""
    forge: str = ""


ForgeCredentialProvider = Callable[[ForgeCredentialRequest], ForgeTokenResolution]


def _unregistered_provider(request: ForgeCredentialRequest) -> ForgeTokenResolution:
    return ForgeTokenResolution(
        request.credential,
        request.overlay_name,
        ForgeTokenState.UNSET,
        detail="forge credential provider is not registered",
    )


_provider: ForgeCredentialProvider = _unregistered_provider


def register_forge_credential_provider(provider: ForgeCredentialProvider) -> None:
    """Install the core-side owning-overlay resolver."""
    global _provider  # noqa: PLW0603 - the single inverted-dependency registration seam
    _provider = provider


def resolve_overlay_token(
    overlay: object,
    *,
    credential: str,
    overlay_name: str = "",
) -> ForgeTokenResolution:
    return _provider(
        ForgeCredentialRequest(
            ForgeCredentialTarget.OVERLAY,
            overlay,
            credential,
            overlay_name=overlay_name,
        )
    )


def resolve_named_overlay_token(overlay_name: str, *, credential: str) -> ForgeTokenResolution:
    return _provider(
        ForgeCredentialRequest(
            ForgeCredentialTarget.NAMED_OVERLAY,
            overlay_name,
            credential,
            overlay_name=overlay_name,
        )
    )


def resolve_repo_token(repo: str, *, credential: str) -> ForgeTokenResolution:
    return _provider(ForgeCredentialRequest(ForgeCredentialTarget.REPO, repo, credential))


def resolve_url_token(url: str, *, credential: str) -> ForgeTokenResolution:
    return _provider(ForgeCredentialRequest(ForgeCredentialTarget.URL, url, credential))


def resolve_slug_token(slug: str, *, forge: str, credential: str) -> ForgeTokenResolution:
    return _provider(
        ForgeCredentialRequest(
            ForgeCredentialTarget.SLUG,
            slug,
            credential,
            forge=forge,
        )
    )


__all__ = [
    "ForgeCredentialRequest",
    "ForgeCredentialTarget",
    "ForgeTokenResolution",
    "ForgeTokenState",
    "register_forge_credential_provider",
    "resolve_named_overlay_token",
    "resolve_overlay_token",
    "resolve_repo_token",
    "resolve_slug_token",
    "resolve_url_token",
]
