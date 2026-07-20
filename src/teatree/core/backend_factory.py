"""Overlay-aware backend factory — resolves config and builds backends.

This module bridges ``teatree.core`` (overlay registry) and
``teatree.backends`` (loader) so that callers in ``core`` and ``cli`` don't
need to extract tokens or branch on platform themselves.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

from teatree.core.backend_protocols import CIService, CodeHostBackend, MessagingBackend
from teatree.core.backend_registry import get_backend_provider

if TYPE_CHECKING:
    from teatree.core.backend_registry import NotionPageClient, SentryReadClient, SharePointReadClient
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_all_overlays, get_overlay
from teatree.core.toml_backends import (
    _apply_voice_classifier_mode,
    _code_host_from_toml_overlay,
    _code_host_from_toml_overlay_for_repo,
    _find_external_db,
    _hosts_from_toml,
    _messaging_from_toml,
    _messaging_from_toml_overlay,
    _toml_messaging_backend,
)
from teatree.types import SharePointRemoteSpec
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OverlayBackends:
    """Backends and config slice for one registered overlay.

    The loop tick builds one set of scanners per ``OverlayBackends`` so a
    user with multiple overlays (e.g. one per GitHub identity) sees PRs,
    issues, and Slack mentions from all of them in one statusline.

    ``hosts`` carries one code-host backend per platform whose token resolved.
    An overlay with both a GitHub and a GitLab PAT exposes both hosts so the
    loop scans both forges (#976). The legacy ``host`` field is exposed as a
    property pointing at ``hosts[0]`` so callers that only consume one
    platform keep working unchanged. ``identities`` carries the user's known
    aliases on the active host (see ``UserSettings.user_identity_aliases``);
    scanners union-query across them.
    """

    name: str
    hosts: tuple[CodeHostBackend, ...] = field(default_factory=tuple)
    messaging: MessagingBackend | None = None
    ready_labels: tuple[str, ...] = field(default_factory=tuple)
    exclude_labels: tuple[str, ...] = ()
    overlay: OverlayBase | None = None
    auto_start_assigned_issues: bool = False
    max_concurrent_auto_starts: int = 1
    stale_threshold_days: int = 3
    external_db: Path | None = None
    identities: tuple[str, ...] = field(default_factory=tuple)

    @property
    def host(self) -> CodeHostBackend | None:
        # Back-compat: callers that pre-date the multi-host migration still
        # consume one host. The first entry is the legacy default — for an
        # overlay with both GitHub and GitLab configured this is GitHub
        # (mirrors ``get_code_host`` precedence).
        return self.hosts[0] if self.hosts else None


_code_host_cache: dict[str, CodeHostBackend] = {}
_messaging_cache: dict[str, MessagingBackend] = {}

# A resolved backend is cached for the process lifetime; a ``None`` result is
# cached only for this brief monotonic window (F4.5). A single transient tick
# where credentials momentarily fail to resolve returned ``None`` — which the
# old permanent-cache pinned for the whole loop's life, disabling the code host /
# messaging until a restart. Re-resolving after a short TTL lets the next tick
# recover on its own; a genuinely-unconfigured overlay just pays a cheap re-read.
_ERROR_NONE_TTL_SECONDS = 30.0
_code_host_none_until: dict[str, float] = {}
_messaging_none_until: dict[str, float] = {}


def _active_overlay_name(overlay_name: str | None) -> str:
    """Resolve the overlay name to use for cache and TOML lookup.

    Explicit *overlay_name* wins over the ``T3_OVERLAY_NAME`` env var; an
    empty string is the canonical "default overlay" cache key for callers
    that rely on single-overlay environments.
    """
    if overlay_name:
        return overlay_name
    return os.environ.get("T3_OVERLAY_NAME", "") or ""


def code_host_from_overlay(overlay_name: str | None = None) -> CodeHostBackend | None:
    """Build a code-host backend using the active overlay's credentials.

    Cached per overlay name for the loop tick — every scanner that needs
    the host shares one instance per process. Tests and wrapper scripts
    that swap overlays must call :func:`reset_backend_caches`.

    *overlay_name* lets a wrapper script select an overlay explicitly
    without mutating ``T3_OVERLAY_NAME``. When omitted, falls back to the
    env var (the same source ``get_overlay()`` reads). Path-only TOML
    overlays (no ``class:`` key) are supported via a TOML fallback so a
    bare ``django.setup()`` resolves the right credentials.
    """
    key = _active_overlay_name(overlay_name)
    if key in _code_host_cache:
        return _code_host_cache[key]
    if _none_still_fresh(_code_host_none_until, key):
        return None
    backend = _build_code_host(key)
    if backend is None:
        _code_host_none_until[key] = time.monotonic() + _ERROR_NONE_TTL_SECONDS
        warn_throttled(
            logger,
            f"code-host-none:{key}",
            "code host for overlay %r resolved to None — re-resolving after %.0fs (not cached for the process life)",
            key or "<default>",
            _ERROR_NONE_TTL_SECONDS,
        )
        return None
    _code_host_cache[key] = backend
    _code_host_none_until.pop(key, None)
    return backend


def _none_still_fresh(deadlines: dict[str, float], key: str) -> bool:
    """Whether a cached ``None`` for *key* is still inside its short TTL window."""
    deadline = deadlines.get(key)
    return deadline is not None and time.monotonic() < deadline


def _build_code_host(overlay_name: str) -> CodeHostBackend | None:
    try:
        overlay = get_overlay(overlay_name or None)
    except ImproperlyConfigured:
        return _code_host_from_toml_overlay(overlay_name)
    return get_backend_provider().get_code_host(overlay)


def code_host_for_repo_from_overlay(repo_path: str, overlay_name: str | None = None) -> CodeHostBackend | None:
    """Build the code-host backend for *repo_path*'s actual origin forge.

    Unlike :func:`code_host_from_overlay` (which selects by token-presence
    precedence), this derives the forge from where the repo physically
    lives — its ``origin`` remote host — so an overlay carrying both a
    GitHub and a GitLab PAT opens the PR on the repo's own forge (#2025).
    Not cached: the result depends on *repo_path*, so two repos under one
    overlay can resolve to different forges. Raises
    :class:`teatree.core.backend_protocols.BackendResolutionError` when the
    repo's forge has no configured credentials.
    """
    key = _active_overlay_name(overlay_name)
    try:
        overlay = get_overlay(key or None)
    except ImproperlyConfigured:
        return _code_host_from_toml_overlay_for_repo(key, repo_path)
    return get_backend_provider().get_code_host_for_repo(overlay, repo_path)


def messaging_from_overlay(overlay_name: str | None = None) -> MessagingBackend | None:
    """Build a messaging backend using the active overlay's config (cached).

    *overlay_name* lets a wrapper script select an overlay explicitly
    without mutating ``T3_OVERLAY_NAME``. When omitted, falls back to the
    env var. Path-only TOML overlays (no ``class:`` key, e.g. an overlay
    declared via ``[overlays.<name>]`` with only a ``path``) are supported
    via a TOML fallback — the same chain ``iter_overlay_backends`` uses —
    so wrapper scripts and bare ``django.setup()`` callers route DMs to
    the correct overlay's Slack bot instead of silently falling back to
    no-backend.
    """
    key = _active_overlay_name(overlay_name)
    if key in _messaging_cache:
        return _messaging_cache[key]
    if _none_still_fresh(_messaging_none_until, key):
        return None
    backend = _build_messaging(key)
    if backend is None:
        _messaging_none_until[key] = time.monotonic() + _ERROR_NONE_TTL_SECONDS
        warn_throttled(
            logger,
            f"messaging-none:{key}",
            "messaging for overlay %r resolved to None — re-resolving after %.0fs (not cached for the process life)",
            key or "<default>",
            _ERROR_NONE_TTL_SECONDS,
        )
        return None
    _messaging_cache[key] = backend
    _messaging_none_until.pop(key, None)
    return backend


def _build_messaging(overlay_name: str) -> MessagingBackend | None:
    try:
        overlay = get_overlay(overlay_name or None)
    except ImproperlyConfigured:
        return _messaging_from_toml_overlay(overlay_name)
    backend = get_backend_provider().get_messaging(overlay)
    _apply_voice_classifier_mode(backend)
    return backend


def configured_messaging_from_overlay(overlay_name: str | None = None) -> MessagingBackend | None:
    """Like :func:`messaging_from_overlay`, but honours the MCP resolver contract (#3299).

    Returns ``None`` when the overlay's ``messaging_backend`` resolves to
    ``"noop"``/empty — i.e. the overlay declares ``Service.SLACK`` but has no
    real messaging transport. ``messaging_from_overlay`` returns a *truthy*
    :class:`~teatree.backends.messaging_noop.NoopMessagingBackend` there, which
    :func:`~teatree.mcp.service_resolver.resolve_declaring_overlay_client` would
    wrongly accept — stopping the search before it reaches the overlay that
    actually carries the Slack credentials. The MCP Slack group passes THIS seam
    to the resolver so the noop declarer is skipped, restoring the resolver's
    documented "``None`` when unconfigured" contract at the source. Every other
    caller keeps the no-``None``-guard :func:`messaging_from_overlay`.
    """
    if _resolved_messaging_backend(overlay_name) in {"", "noop"}:
        return None
    return messaging_from_overlay(overlay_name)


def _resolved_messaging_backend(overlay_name: str | None) -> str:
    """The overlay's effective ``messaging_backend`` choice (``""`` when unresolvable)."""
    key = _active_overlay_name(overlay_name)
    try:
        overlay = get_overlay(key or None)
    except ImproperlyConfigured:
        return _toml_messaging_backend(key)
    return overlay.config.messaging_backend or ""


def ci_service_from_overlay(overlay_name: str | None = None) -> CIService | None:
    """Build a CI-service backend using the active overlay's credentials."""
    key = _active_overlay_name(overlay_name)
    try:
        overlay = get_overlay(key or None)
    except ImproperlyConfigured:
        return None

    return get_backend_provider().get_ci_service(
        gitlab_token=overlay.config.get_gitlab_token(),
        gitlab_url=overlay.config.gitlab_url,
    )


def notion_client_from_overlay(overlay_name: str | None = None) -> "NotionPageClient | None":
    """Build a direct-Notion API client from the active overlay's token.

    Returns ``None`` when no ``notion_token`` resolves (the default-safe posture
    — the runtime status-sync then no-ops). Mirrors :func:`messaging_from_overlay`
    but stays uncached: the client holds no live connection, and skipping the
    cache avoids cross-overlay token bleed in tests.
    """
    key = _active_overlay_name(overlay_name)
    try:
        overlay = get_overlay(key or None)
    except ImproperlyConfigured:
        return None
    token = overlay.config.get_notion_token()
    if not token:
        return None
    return get_backend_provider().build_notion_client(token=token)


def sentry_client_from_overlay(overlay_name: str | None = None) -> "SentryReadClient | None":
    """Build a read-only Sentry client from the active overlay's config.

    Returns ``None`` when the overlay declares no ``sentry_org`` (the
    default-safe posture — the sentry MCP group's resolver then moves to the next
    declaring overlay or fails loud). Mirrors :func:`notion_client_from_overlay`:
    resolved through the registered provider so ``core`` never imports the
    concrete ``teatree.backends.sentry`` client. Uncached — the client holds no
    live connection.
    """
    key = _active_overlay_name(overlay_name)
    try:
        overlay = get_overlay(key or None)
    except ImproperlyConfigured:
        return None
    config = overlay.config
    if not config.sentry_org:
        return None
    return get_backend_provider().build_sentry_client(
        token=config.get_sentry_token(),
        org=config.sentry_org,
        base_url=config.sentry_url,
    )


def sharepoint_client_from_overlay(overlay_name: str | None = None) -> "SharePointReadClient | None":
    """Build a read-only SharePoint/OneDrive client from the environment (#3084).

    The remote's tenant/site/root values are client-specific and must stay out of
    this public repo, so they are read from the ``TEATREE_SHAREPOINT_*`` wrapper
    environment (set by the private skill/overlay), NOT from committed config —
    mirroring the issue's env-var fetch helper. Returns ``None`` when
    ``TEATREE_SHAREPOINT_REMOTE`` is unset (the default-safe posture — the
    sharepoint MCP group's resolver then moves to the next declaring overlay or
    fails loud). Resolved through the registered provider so ``core`` never
    imports the concrete ``teatree.backends.sharepoint`` client. The
    ``overlay_name`` argument keeps the resolver's ``build(name)`` contract; the
    gate that a registered overlay must DECLARE the service still holds upstream.
    """
    del overlay_name  # config is env-scoped, not per-overlay; gating is upstream.
    remote = os.environ.get("TEATREE_SHAREPOINT_REMOTE", "")
    if not remote:
        return None
    return get_backend_provider().build_sharepoint_client(
        SharePointRemoteSpec(
            remote=remote,
            root=os.environ.get("TEATREE_SHAREPOINT_ROOT", ""),
            config_path=os.environ.get("TEATREE_SHAREPOINT_CONFIG", ""),
            password_command=os.environ.get("TEATREE_SHAREPOINT_PASSWORD_COMMAND", ""),
            site_url=os.environ.get("TEATREE_SHAREPOINT_SITE_URL", ""),
            library_path=os.environ.get("TEATREE_SHAREPOINT_LIBRARY_PATH", ""),
        ),
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
    identities = _resolved_identities()
    provider = get_backend_provider()

    for name, overlay in get_all_overlays().items():
        found_names.add(name)
        try:
            hosts = tuple(provider.get_code_hosts(overlay))
        except (ImproperlyConfigured, ValueError):
            hosts = ()
        try:
            messaging = provider.get_messaging(overlay)
        except (ImproperlyConfigured, ValueError):
            messaging = None
        out.append(
            OverlayBackends(
                name=name,
                hosts=hosts,
                messaging=messaging,
                ready_labels=tuple(overlay.config.ready_labels),
                exclude_labels=tuple(overlay.config.exclude_labels),
                overlay=overlay,
                auto_start_assigned_issues=bool(overlay.config.auto_start_assigned_issues),
                max_concurrent_auto_starts=int(overlay.config.max_concurrent_auto_starts),
                stale_threshold_days=int(overlay.config.stale_threshold_days),
                identities=identities,
            ),
        )

    out.extend(_backends_from_toml(found_names, identities))
    return out


def _resolved_identities() -> tuple[str, ...]:
    """Return the user's configured identity aliases.

    Each entry is one handle/login the user owns across forges. The loop
    scanners union-query across them so PRs/MRs authored or reviewer-tagged
    under any alias surface in the statusline (#976). Empty list keeps the
    legacy behaviour: scanners scan only ``host.current_user()``.

    Source of truth: ``UserSettings.user_identity_aliases`` — DB-home (#1775),
    resolved via the effective-settings tier (``config_setting set
    user_identity_aliases '[...]'``). Reading through ``get_effective_settings``
    means every consumer agrees on the parsed shape and sees the DB value.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: call-time import, kept lazy

    return tuple(get_effective_settings().user_identity_aliases)


def _backends_from_toml(
    already_found: set[str],
    identities: tuple[str, ...] = (),
) -> list[OverlayBackends]:
    """Build backends for TOML overlays not discovered via entry points."""
    from teatree.config import load_config  # noqa: PLC0415 — deferred: call-time import, kept lazy

    result: list[OverlayBackends] = []
    config = load_config()
    for name, overlay_cfg in (config.raw.get("overlays") or {}).items():
        if name in already_found or not isinstance(overlay_cfg, dict):
            continue
        hosts = tuple(_hosts_from_toml(overlay_cfg))
        messaging = _messaging_from_toml(overlay_cfg)
        db_path = _find_external_db(name, overlay_cfg)
        if not hosts and messaging is None and db_path is None:
            continue
        result.append(
            OverlayBackends(
                name=name,
                hosts=hosts,
                messaging=messaging,
                ready_labels=tuple(overlay_cfg.get("ready_labels", ())),
                exclude_labels=tuple(overlay_cfg.get("exclude_labels", ())),
                stale_threshold_days=int(overlay_cfg.get("stale_threshold_days", 3)),
                external_db=db_path,
                identities=identities,
            ),
        )
    return result


def reset_backend_caches() -> None:
    """Clear all per-overlay backend caches.

    Call when the active overlay changes (overlay reload, multi-overlay
    test fixtures) so the next factory call rebuilds with fresh credentials.
    """
    _code_host_cache.clear()
    _messaging_cache.clear()
    _code_host_none_until.clear()
    _messaging_none_until.clear()
    get_backend_provider().reset_caches()


__all__ = [
    "OverlayBackends",
    "ci_service_from_overlay",
    "code_host_for_repo_from_overlay",
    "code_host_from_overlay",
    "configured_messaging_from_overlay",
    "iter_overlay_backends",
    "messaging_from_overlay",
    "notion_client_from_overlay",
    "reset_backend_caches",
    "sentry_client_from_overlay",
    "sharepoint_client_from_overlay",
]
