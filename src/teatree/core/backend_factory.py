"""Overlay-aware backend factory — resolves config and builds backends.

This module bridges ``teatree.core`` (overlay registry) and
``teatree.backends`` (loader) so that callers in ``core`` and ``cli`` don't
need to extract tokens or branch on platform themselves.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from teatree.core.backend_protocols import BackendResolutionError, CIService, CodeHostBackend, MessagingBackend
from teatree.core.backend_registry import get_backend_provider
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_all_overlays, get_overlay
from teatree.paths import find_overlay_db
from teatree.utils import git
from teatree.utils.forge import forge_from_remote


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


_code_host_cache: dict[str, CodeHostBackend | None] = {}
_messaging_cache: dict[str, MessagingBackend | None] = {}


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
    backend = _build_code_host(key)
    _code_host_cache[key] = backend
    return backend


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
    backend = _build_messaging(key)
    _messaging_cache[key] = backend
    return backend


def _build_messaging(overlay_name: str) -> MessagingBackend | None:
    try:
        overlay = get_overlay(overlay_name or None)
    except ImproperlyConfigured:
        return _messaging_from_toml_overlay(overlay_name)
    backend = get_backend_provider().get_messaging(overlay)
    _apply_voice_classifier_mode(backend)
    return backend


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


def _messaging_from_toml_overlay(overlay_name: str) -> MessagingBackend | None:
    """Build a messaging backend from a path-only TOML overlay entry.

    Used by the fallback in :func:`messaging_from_overlay` so wrapper
    scripts that opt into an overlay without a registered Python class
    still route to its credentials. Mirrors the discovery shape of
    ``_backends_from_toml``.
    """
    if not overlay_name:
        return None
    from teatree.config import load_config  # noqa: PLC0415

    overlays = load_config().raw.get("overlays") or {}
    cfg = overlays.get(overlay_name)
    if not isinstance(cfg, dict):
        return None
    return _messaging_from_toml(cfg)


def _code_host_from_toml_overlay(overlay_name: str) -> CodeHostBackend | None:
    """Build a code-host backend from a path-only TOML overlay entry."""
    if not overlay_name:
        return None
    from teatree.config import load_config  # noqa: PLC0415

    overlays = load_config().raw.get("overlays") or {}
    cfg = overlays.get(overlay_name)
    if not isinstance(cfg, dict):
        return None
    return _host_from_toml(cfg)


def _code_host_from_toml_overlay_for_repo(overlay_name: str, repo_path: str) -> CodeHostBackend | None:
    """Per-repo code host from a path-only TOML overlay entry (#2025).

    The path-only fallback must derive the forge from *repo_path*'s origin
    host too — otherwise the original #2025 token-precedence bug survives
    for TOML-only overlays (``_host_from_toml`` is GitHub-first).
    """
    if not overlay_name:
        return None
    from teatree.config import load_config  # noqa: PLC0415

    overlays = load_config().raw.get("overlays") or {}
    cfg = overlays.get(overlay_name)
    if not isinstance(cfg, dict):
        return None
    return _host_from_toml_for_repo(cfg, repo_path)


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
    from teatree.config import get_effective_settings  # noqa: PLC0415

    return tuple(get_effective_settings().user_identity_aliases)


def _backends_from_toml(
    already_found: set[str],
    identities: tuple[str, ...] = (),
) -> list[OverlayBackends]:
    """Build backends for TOML overlays not discovered via entry points."""
    from teatree.config import load_config  # noqa: PLC0415

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


def _find_external_db(name: str, cfg: dict) -> Path | None:
    project_path = cfg.get("path", "")
    if not project_path:
        return None
    return find_overlay_db(name, project_path)


def _hosts_from_toml(cfg: dict) -> list[CodeHostBackend]:
    """Return every code-host backend a TOML overlay opts into.

    Pre-#976 the loop only constructed one host per TOML overlay, so an
    entry with both ``gitlab_token_ref`` and ``github_token_ref`` silently
    dropped one platform. Build both when both resolve so the loop can
    scan each forge independently.
    """
    from teatree.utils.secrets import read_pass  # noqa: PLC0415

    provider = get_backend_provider()
    hosts: list[CodeHostBackend] = []
    github_token_ref = cfg.get("github_token_ref", "")
    if github_token_ref:
        token = read_pass(github_token_ref)
        if token:
            hosts.append(provider.build_github_host(token=token))

    gitlab_token_ref = cfg.get("gitlab_token_ref", "")
    gitlab_url = cfg.get("gitlab_url", "https://gitlab.com")
    if gitlab_token_ref:
        token = read_pass(gitlab_token_ref)
        if token:
            hosts.append(provider.build_gitlab_host(token=token, base_url=gitlab_url))
    return hosts


def _host_from_toml(cfg: dict) -> CodeHostBackend | None:
    """Single-host shim — first matching host per TOML overlay.

    Pre-#976 callers consumed exactly one host per TOML overlay. Kept so
    code paths outside the loop scanner stack don't need to learn the
    multi-host shape just to read out the legacy default.
    """
    hosts = _hosts_from_toml(cfg)
    return hosts[0] if hosts else None


def _host_from_toml_for_repo(cfg: dict, repo_path: str) -> CodeHostBackend | None:
    """Build the TOML overlay's host for *repo_path*'s origin forge (#2025).

    Mirrors :func:`teatree.backends.loader.get_code_host_for_repo` for the
    path-only TOML overlay: the forge is the repo's origin host, not
    token-presence order. Raises :class:`BackendResolutionError` when the
    repo's forge has no token ref configured on the overlay; falls back to
    the overlay default only when the repo has no origin / an unrecognised
    host.
    """
    from teatree.utils.secrets import read_pass  # noqa: PLC0415

    remote = git.remote_url(repo=repo_path)
    forge = forge_from_remote(remote) if remote else ""
    if not forge:
        return _host_from_toml(cfg)

    provider = get_backend_provider()
    if forge == "github":
        github_token_ref = cfg.get("github_token_ref", "")
        token = read_pass(github_token_ref) if github_token_ref else ""
        if token:
            return provider.build_github_host(token=token)
    else:
        gitlab_token_ref = cfg.get("gitlab_token_ref", "")
        token = read_pass(gitlab_token_ref) if gitlab_token_ref else ""
        if token:
            return provider.build_gitlab_host(token=token, base_url=cfg.get("gitlab_url", "https://gitlab.com"))

    msg = (
        f"repo origin resolves to the {forge} forge ({remote!r}) but the TOML overlay "
        f"has no {forge} token configured — cannot open a PR. "
        f"Configure {forge}_token_ref for this overlay."
    )
    raise BackendResolutionError(msg)


def _messaging_from_toml(cfg: dict) -> MessagingBackend | None:
    if cfg.get("messaging_backend") != "slack":
        return None
    from teatree.utils.secrets import read_pass  # noqa: PLC0415

    token_ref = cfg.get("slack_token_ref", "")
    if not token_ref:
        return None
    bot_token = read_pass(f"{token_ref}-bot")
    app_token = read_pass(f"{token_ref}-app")
    user_token_ref = cfg.get("user_token_ref", "")
    user_token = read_pass(user_token_ref) if user_token_ref else ""
    user_id = cfg.get("slack_user_id", "")
    # Setup-time provisioned IM channel id (#1342). When set, threads into
    # the Slack bot so its ``open_dm`` short-circuits the live
    # ``conversations.open`` for the configured user, routing DMs through this
    # bot's IM instead of failing ``channel_not_found``.
    dm_channel_id = cfg.get("slack_dm_channel_id", "")
    if bot_token:
        # Loop construction path — a malformed user token degrades to
        # bot-only instead of crashing the tick (see ``get_messaging``).
        backend = get_backend_provider().build_slack_messaging(
            bot_token=bot_token,
            app_token=app_token or "",
            user_token=user_token,
            user_id=user_id,
            dm_channel_id=dm_channel_id,
        )
        _apply_voice_classifier_mode(backend)
        return backend
    return None


def _apply_voice_classifier_mode(backend: "MessagingBackend | None") -> None:
    """Resolve the voice/token classifier mode from config (#1395).

    Reads the effective setting (env / per-overlay / global) and
    threads it into a :class:`SlackBotBackend` via its setter. Noop
    backends and missing-credentials cases are skipped. Tolerates
    fake configs that don't carry a ``user`` attribute (path-only TOML
    fallback test fixtures) by leaving the backend on its default
    :attr:`SlackVoiceClassifierMode.WARN`.
    """
    setter = getattr(backend, "set_voice_classifier_mode", None)
    if setter is None or not callable(setter):
        return
    try:
        from teatree.config import get_effective_settings  # noqa: PLC0415

        setter(get_effective_settings().slack_voice_classifier_mode)
    except (AttributeError, ImportError):
        return


def reset_backend_caches() -> None:
    """Clear all per-overlay backend caches.

    Call when the active overlay changes (overlay reload, multi-overlay
    test fixtures) so the next factory call rebuilds with fresh credentials.
    """
    _code_host_cache.clear()
    _messaging_cache.clear()
    get_backend_provider().reset_caches()


__all__ = [
    "OverlayBackends",
    "ci_service_from_overlay",
    "code_host_for_repo_from_overlay",
    "code_host_from_overlay",
    "iter_overlay_backends",
    "messaging_from_overlay",
    "reset_backend_caches",
]
