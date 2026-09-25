"""Core-side provider for the import-safe forge credential seam."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

from teatree.config.credential_pass_key import PassKeyResolution, PassKeySource, resolve_pass_key
from teatree.core.overlay_name_resolution import overlay_name_of
from teatree.core.overlays.overlay_namespace import namespace_owner
from teatree.forge_credentials import (
    ForgeCredentialRequest,
    ForgeCredentialTarget,
    ForgeTokenResolution,
    ForgeTokenState,
    register_forge_credential_provider,
)
from teatree.utils import git
from teatree.utils.secrets import read_pass

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

OverlayGetter = Callable[[str | None], "OverlayBase"]
RepoOverlayGetter = Callable[[str], "OverlayBase | None"]
OverlayInferrer = Callable[[str], str | None]
OverlayNames = Callable[[], Iterable[str]]
OwnedRepos = Callable[[str], dict[str, list[str]]]


def build_and_register(
    *,
    get_overlay: OverlayGetter,
    get_overlay_for_repo: RepoOverlayGetter,
    infer_overlay_for_url: OverlayInferrer,
    all_overlay_names: OverlayNames,
    owned_repos: OwnedRepos,
) -> None:
    """Register the state-preserving resolver against the live overlay registry."""
    register_forge_credential_provider(
        _RegisteredProvider(
            get_overlay=get_overlay,
            get_overlay_for_repo=get_overlay_for_repo,
            infer_overlay_for_url=infer_overlay_for_url,
            all_overlay_names=all_overlay_names,
            owned_repos=owned_repos,
        )
    )


@dataclass(frozen=True, slots=True)
class _RegisteredProvider:
    get_overlay: OverlayGetter
    get_overlay_for_repo: RepoOverlayGetter
    infer_overlay_for_url: OverlayInferrer
    all_overlay_names: OverlayNames
    owned_repos: OwnedRepos

    def __call__(self, request: ForgeCredentialRequest) -> ForgeTokenResolution:
        handlers = {
            ForgeCredentialTarget.OVERLAY: self._overlay,
            ForgeCredentialTarget.NAMED_OVERLAY: self._named,
            ForgeCredentialTarget.REPO: self._repo,
            ForgeCredentialTarget.URL: self._url,
            ForgeCredentialTarget.SLUG: self._slug,
        }
        return handlers[request.target_kind](request)

    @staticmethod
    def _overlay(request: ForgeCredentialRequest) -> ForgeTokenResolution:
        return _resolve_overlay(request.target, request.credential, request.overlay_name)

    def _named(self, request: ForgeCredentialRequest) -> ForgeTokenResolution:
        return self._resolve_named(str(request.target), request.credential)

    def _repo(self, request: ForgeCredentialRequest) -> ForgeTokenResolution:
        repo = str(request.target)
        remote = git.remote_url(repo=repo)
        name = self.infer_overlay_for_url(remote) if remote else ""
        if not name:
            try:
                name = overlay_name_of(self.get_overlay_for_repo(repo))
            except Exception:  # noqa: BLE001 - unresolved ownership fails closed
                name = ""
        return self._resolve_owner(name, request.credential, "no overlay owns the repository")

    def _url(self, request: ForgeCredentialRequest) -> ForgeTokenResolution:
        return self._resolve_owner(
            self.infer_overlay_for_url(str(request.target)) or "",
            request.credential,
            "no overlay owns the forge URL",
        )

    def _slug(self, request: ForgeCredentialRequest) -> ForgeTokenResolution:
        name = _slug_owner(
            str(request.target),
            forge=request.forge,
            infer_overlay_for_url=self.infer_overlay_for_url,
            all_overlay_names=self.all_overlay_names,
            owned_repos=self.owned_repos,
        )
        return self._resolve_owner(name, request.credential, "no overlay owns the repository")

    def _resolve_named(self, name: str, credential: str) -> ForgeTokenResolution:
        try:
            overlay = self.get_overlay(name or None)
        except ImproperlyConfigured:
            route = resolve_pass_key(credential, overlay_name=name, declared_default="")
            return _read_routed_token(credential=credential, overlay_name=name, route=route)
        return _resolve_overlay(overlay, credential, name)

    def _resolve_owner(self, name: str, credential: str, detail: str) -> ForgeTokenResolution:
        if not name:
            return ForgeTokenResolution(credential, "", ForgeTokenState.UNSET, detail=detail)
        return self._resolve_named(name, credential)


def _resolve_overlay(overlay: "OverlayBase", credential: str, overlay_name: str) -> ForgeTokenResolution:
    name = overlay_name or overlay_name_of(overlay)
    return _read_routed_token(
        credential=credential,
        overlay_name=name,
        route=overlay.config.resolve_pass_key(credential),
    )


def _slug_owner(
    slug: str,
    *,
    forge: str,
    infer_overlay_for_url: OverlayInferrer,
    all_overlay_names: OverlayNames,
    owned_repos: OwnedRepos,
) -> str:
    if inferred := infer_overlay_for_url(slug):
        return inferred
    try:
        scopes = [(name, owned_repos(name)) for name in all_overlay_names()]
    except Exception:  # noqa: BLE001 - unreadable ownership must never pick a token
        return ""
    return namespace_owner(slug, scopes, forge=forge)


def _read_routed_token(
    *,
    credential: str,
    overlay_name: str,
    route: PassKeyResolution,
) -> ForgeTokenResolution:
    if route.source is PassKeySource.UNREADABLE:
        return ForgeTokenResolution(
            credential,
            overlay_name,
            ForgeTokenState.UNREADABLE,
            route_source=route.source,
            detail=f"{route.setting} is unreadable ({route.source})",
        )
    if not route.value:
        return ForgeTokenResolution(
            credential,
            overlay_name,
            ForgeTokenState.UNSET,
            route_source=route.source,
            detail=f"{route.setting} is unset",
        )
    try:
        token = read_pass(route.value)
    except Exception as exc:  # noqa: BLE001 - preserve every secret-store failure
        return ForgeTokenResolution(
            credential,
            overlay_name,
            ForgeTokenState.UNREADABLE,
            pass_key=route.value,
            route_source=route.source,
            detail=f"{route.setting} routes to {route.value!r}, but the secret store is unreadable: {exc}",
        )
    if not token:
        return ForgeTokenResolution(
            credential,
            overlay_name,
            ForgeTokenState.UNSET,
            pass_key=route.value,
            route_source=route.source,
            detail=f"{route.setting} routes to {route.value!r}, but that entry is absent or empty",
        )
    return ForgeTokenResolution(
        credential,
        overlay_name,
        ForgeTokenState.TOKEN,
        token=token,
        pass_key=route.value,
        route_source=route.source,
    )
