"""Build backends from a path-only TOML overlay entry.

A TOML-configured overlay (``[overlays.<name>]`` with a ``path`` but no Python
``class:``) still carries credentials — its GitHub/GitLab token refs, Slack
token ref, and ``gitlab_url``. This module turns that raw config block into
concrete backends so wrapper scripts and bare ``django.setup()`` callers route
to the right forge / Slack bot. Split out of ``backend_factory`` so the factory
keeps the caching + overlay-registry orchestration and this holds the TOML
plumbing; ``backend_factory`` re-imports these leaves and remains the single
public surface (and ``mock.patch`` target) for the loop.
"""

from collections.abc import Mapping
from pathlib import Path

from teatree.core.backend_protocols import BackendResolutionError, CodeHostBackend, MessagingBackend
from teatree.core.backend_registry import get_backend_provider
from teatree.paths import find_overlay_db
from teatree.utils import git
from teatree.utils.forge import forge_from_remote

type OverlayTomlConfig = Mapping[str, object]
"""One overlay's parsed TOML config block (``[overlays.<name>]``) — string keys,
heterogeneous values (strings, lists, nested tables). Read-only at every callsite."""


def _find_external_db(name: str, cfg: OverlayTomlConfig) -> Path | None:
    project_path = cfg.get("path", "")
    if not isinstance(project_path, str) or not project_path:
        return None
    return find_overlay_db(name, project_path)


def _cfg_str(cfg: OverlayTomlConfig, key: str, default: str = "") -> str:
    """Read a string config value from a heterogeneous TOML overlay block.

    ``OverlayTomlConfig`` values are ``object`` (TOML admits strings, ints,
    lists, tables), so a raw ``cfg.get`` cannot be handed to a ``str``-typed
    seam. This narrows to ``str`` — a non-string / absent value falls back to
    *default* — giving every token/url read one typed accessor.
    """
    value = cfg.get(key, default)
    return value if isinstance(value, str) else default


def _hosts_from_toml(cfg: OverlayTomlConfig) -> list[CodeHostBackend]:
    """Return every code-host backend a TOML overlay opts into.

    Pre-#976 the loop only constructed one host per TOML overlay, so an
    entry with both ``gitlab_token_ref`` and ``github_token_ref`` silently
    dropped one platform. Build both when both resolve so the loop can
    scan each forge independently.
    """
    from teatree.core.send_proxy import read_posting_credential  # noqa: PLC0415 — deferred: ORM model, pre-app-registry

    provider = get_backend_provider()
    hosts: list[CodeHostBackend] = []
    github_token_ref = _cfg_str(cfg, "github_token_ref")
    if github_token_ref:
        token = read_posting_credential(github_token_ref)
        if token:
            hosts.append(provider.build_github_host(token=token))

    gitlab_token_ref = _cfg_str(cfg, "gitlab_token_ref")
    gitlab_url = _cfg_str(cfg, "gitlab_url", "https://gitlab.com")
    if gitlab_token_ref:
        token = read_posting_credential(gitlab_token_ref)
        if token:
            hosts.append(provider.build_gitlab_host(token=token, base_url=gitlab_url))
    return hosts


def _host_from_toml(cfg: OverlayTomlConfig) -> CodeHostBackend | None:
    """Single-host shim — first matching host per TOML overlay.

    Pre-#976 callers consumed exactly one host per TOML overlay. Kept so
    code paths outside the loop scanner stack don't need to learn the
    multi-host shape just to read out the legacy default.
    """
    hosts = _hosts_from_toml(cfg)
    return hosts[0] if hosts else None


def _host_from_toml_for_repo(cfg: OverlayTomlConfig, repo_path: str) -> CodeHostBackend | None:
    """Build the TOML overlay's host for *repo_path*'s origin forge (#2025).

    Mirrors :func:`teatree.backends.loader.get_code_host_for_repo` for the
    path-only TOML overlay: the forge is the repo's origin host, not
    token-presence order. Raises :class:`BackendResolutionError` when the
    repo's forge has no token ref configured on the overlay; falls back to
    the overlay default only when the repo has no origin / an unrecognised
    host.
    """
    from teatree.core.send_proxy import read_posting_credential  # noqa: PLC0415 — deferred: ORM model, pre-app-registry

    remote = git.remote_url(repo=repo_path)
    forge = forge_from_remote(remote) if remote else ""
    if not forge:
        return _host_from_toml(cfg)

    provider = get_backend_provider()
    if forge == "github":
        github_token_ref = _cfg_str(cfg, "github_token_ref")
        token = read_posting_credential(github_token_ref)
        if token:
            return provider.build_github_host(token=token)
    else:
        gitlab_token_ref = _cfg_str(cfg, "gitlab_token_ref")
        token = read_posting_credential(gitlab_token_ref)
        if token:
            return provider.build_gitlab_host(token=token, base_url=_cfg_str(cfg, "gitlab_url", "https://gitlab.com"))

    msg = (
        f"repo origin resolves to the {forge} forge ({remote!r}) but the TOML overlay "
        f"has no {forge} token configured — cannot open a PR. "
        f"Configure {forge}_token_ref for this overlay."
    )
    raise BackendResolutionError(msg)


def _messaging_from_toml(cfg: OverlayTomlConfig) -> MessagingBackend | None:
    if cfg.get("messaging_backend") != "slack":
        return None
    from teatree.core.messaging_tokens import resolve_messaging_tokens  # noqa: PLC0415 — deferred: pre-app-registry

    token_ref = _cfg_str(cfg, "slack_token_ref")
    if not token_ref:
        return None
    tokens = resolve_messaging_tokens(slack_token_ref=token_ref, user_token_ref=_cfg_str(cfg, "user_token_ref"))
    bot_token = tokens.bot
    app_token = tokens.app
    user_token = tokens.user
    user_id = _cfg_str(cfg, "slack_user_id")
    # Setup-time provisioned IM channel id (#1342). When set, threads into
    # the Slack bot so its ``open_dm`` short-circuits the live
    # ``conversations.open`` for the configured user, routing DMs through this
    # bot's IM instead of failing ``channel_not_found``.
    dm_channel_id = _cfg_str(cfg, "slack_dm_channel_id")
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
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: call-time import, kept lazy

        setter(get_effective_settings().slack_voice_classifier_mode)
    except (AttributeError, ImportError):
        return


def _toml_messaging_backend(overlay_name: str) -> str:
    """The ``messaging_backend`` value of a path-only TOML overlay entry (``""`` when absent)."""
    if not overlay_name:
        return ""
    from teatree.config import load_config  # noqa: PLC0415 — deferred: call-time import, kept lazy

    overlays = load_config().raw.get("overlays") or {}
    cfg = overlays.get(overlay_name)
    if not isinstance(cfg, dict):
        return ""
    return str(cfg.get("messaging_backend", "") or "")


def _messaging_from_toml_overlay(overlay_name: str) -> MessagingBackend | None:
    """Build a messaging backend from a path-only TOML overlay entry.

    Used by the fallback in :func:`teatree.core.backend_factory.messaging_from_overlay`
    so wrapper scripts that opt into an overlay without a registered Python
    class still route to its credentials. Mirrors the discovery shape of
    ``_backends_from_toml``.
    """
    cfg = _overlay_cfg(overlay_name)
    return _messaging_from_toml(cfg) if cfg is not None else None


def _code_host_from_toml_overlay(overlay_name: str) -> CodeHostBackend | None:
    """Build a code-host backend from a path-only TOML overlay entry."""
    cfg = _overlay_cfg(overlay_name)
    return _host_from_toml(cfg) if cfg is not None else None


def _code_host_from_toml_overlay_for_repo(overlay_name: str, repo_path: str) -> CodeHostBackend | None:
    """Per-repo code host from a path-only TOML overlay entry (#2025).

    The path-only fallback must derive the forge from *repo_path*'s origin
    host too — otherwise the original #2025 token-precedence bug survives
    for TOML-only overlays (``_host_from_toml`` is GitHub-first).
    """
    cfg = _overlay_cfg(overlay_name)
    return _host_from_toml_for_repo(cfg, repo_path) if cfg is not None else None


def _overlay_cfg(overlay_name: str) -> OverlayTomlConfig | None:
    """Return the raw ``[overlays.<name>]`` config block, or ``None`` when absent."""
    if not overlay_name:
        return None
    from teatree.config import load_config  # noqa: PLC0415 — deferred: call-time import, kept lazy

    overlays = load_config().raw.get("overlays") or {}
    cfg = overlays.get(overlay_name)
    return cfg if isinstance(cfg, dict) else None
