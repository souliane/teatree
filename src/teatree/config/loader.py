"""TeaTree config loading — ``load_config`` + the logging/dir entry points.

``load_config`` (builds ``TeaTreeConfig`` from the DB), the default Django LOGGING
dict, ``load_e2e_repos`` / ``load_peer_instances``, and the ``clone_root`` / ``worktree_root`` /
``check_for_updates`` resolvers. Split out of the package
facade for the RUF067 init-is-re-exports-only rule; re-exported from
``teatree.config`` so every ``teatree.config.<name>`` path stays valid. The
per-setting resolvers live in ``resolution`` and are reached through the package
facade at call-time (the partition's loader -> resolution edge, deferred to avoid
the loader/resolution/discovery import cycle).

Every ``UserSettings`` field is DB-home: its authoritative value comes from the
``ConfigSetting`` store (global + per-overlay rows) + the ``T3_*`` env layer,
resolved per-field by ``resolution.get_effective_settings``. ``load_config`` builds
only the NON-settings registry tables (``REGISTRY_KEYS``) into
``config.raw`` — themselves DB-home, read from the ``ConfigSetting`` store by
``_inject_db_registries``. There is no config file: an install is fully configured
from the DB. Callers that need effective settings must use ``get_effective_settings``,
not the bare ``load_config().user`` (which is always the dataclass defaults).
"""

import logging
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

import teatree.config as _facade
from teatree.config.e2e_repo import E2ERepo
from teatree.config.peer_instance import (
    DEFAULT_REMOTE_PORT,
    DEFAULT_TIMEOUT_SECONDS,
    PeerInstance,
    PeerTransport,
    PeerTunnel,
)
from teatree.config.settings import TeaTreeConfig, UserSettings
from teatree.paths import get_data_dir
from teatree.update_check import run_update_check

_logger = logging.getLogger("teatree.config")


def default_logging(namespace: str) -> dict:
    """Return a default Django LOGGING dict that writes to ``<data_dir>/logs/teatree.log``.

    Usage in settings::

        from teatree.config import default_logging
        LOGGING = default_logging("my_overlay")
    """
    log_dir = get_data_dir(namespace) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "verbose": {
                "format": "{asctime} {levelname} {name} {message}",
                "style": "{",
            },
        },
        "handlers": {
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "teatree.log"),
                "maxBytes": 5_000_000,
                "backupCount": 3,
                "formatter": "verbose",
            },
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "verbose",
            },
        },
        "root": {
            "handlers": ["console", "file"],
            "level": "INFO",
        },
        "loggers": {
            "django.request": {"level": "INFO", "propagate": True},
            "teatree": {"level": "DEBUG", "propagate": True},
        },
    }


# The registry tables: NON-``UserSettings`` config read directly off ``config.raw``
# at many call sites — the ``overlays`` definition registry (``discover_overlays`` +
# every ``raw["overlays"]`` reader), the ``e2e_repos`` registry (``load_e2e_repos``)
# and the ``peer_instances`` registry (``load_peer_instances``). Each is DB-home:
# stored as a single JSON-dict ``ConfigSetting`` row (``REGISTRY_SETTINGS``), injected
# into ``raw`` here so every existing reader is untouched. With no row the table is
# simply absent (empty).


_REGISTRY_READ_DEGRADED_ADVISORY = (
    "teatree: the config store could not be read, so the overlays / e2e-repos registries are "
    "resolving as EMPTY — an overlay you have configured may be invisible to this process. "
    "Run `t3 doctor check` to see whether the config tier is healthy."
)


def _inject_db_registries(raw: dict) -> None:
    """Populate ``raw[<key>]`` from its DB row, for every ``REGISTRY_KEYS`` member.

    Read via the Django-free ``cold_reader`` (the same store ``config_setting`` writes)
    because ``load_config`` runs pre-Django on the overlay-discovery path. Fail-open: a
    missing DB / row leaves the key absent, so a not-yet-configured install still boots
    (with no overlays / e2e repos) rather than raising.

    A store that could not be READ AT ALL is the same empty answer to the caller — boot must
    not raise here — but never the same SILENCE: an unreadable store makes every configured
    overlay vanish, which presents as "unknown overlay" rather than as the fault it is. So the
    confirming read is what is asked for, and a failed one is recorded for ``t3 doctor`` and
    announced once on stderr.
    """
    from teatree.config import cold_reader  # noqa: PLC0415 — Django-free DB read on the pre-Django discovery path
    from teatree.config.host_projection import warn_once  # noqa: PLC0415 — deferred with its cold-read siblings
    from teatree.config.override_read_health import (  # noqa: PLC0415 — deferred with its cold-read siblings
        record_degraded_read,
    )
    from teatree.config.registries import REGISTRY_KEYS  # noqa: PLC0415 — deferred: breaks loader ↔ registries cycle

    for key in REGISTRY_KEYS:
        read = cold_reader.read_setting_confirmed(key)
        if not read.readable:
            record_degraded_read("", caller=f"config.loader._inject_db_registries[{key}]")
            warn_once(_REGISTRY_READ_DEGRADED_ADVISORY)
            continue
        if isinstance(read.value, dict):
            raw[key] = read.value


def load_config() -> TeaTreeConfig:
    """Build the config from the DB — ``user`` is the dataclass defaults, ``raw`` the registries.

    Every ``UserSettings`` field is DB-home, so ``user`` here is always the plain
    dataclass defaults; a caller that needs effective values uses
    ``get_effective_settings`` (which layers the ``ConfigSetting`` store + env). The
    ``raw`` dict carries only the NON-settings registry tables (``REGISTRY_KEYS``),
    injected from the store by :func:`_inject_db_registries`, so an
    install with no config file boots a fully DB-configured teatree.
    """
    raw: dict = {}
    _inject_db_registries(raw)
    return TeaTreeConfig(user=UserSettings(), raw=raw)


def load_e2e_repos() -> list[E2ERepo]:
    """Load named E2E repos from the ``e2e_repos`` registry (DB-home).

    Reads ``config.raw["e2e_repos"]`` — which ``load_config`` populates from the
    DB-home ``e2e_repos`` ``ConfigSetting`` row (``_inject_db_registries``). Each
    entry may specify ``url``, ``branch``, and optionally ``e2e_dir`` (the
    subdirectory containing ``playwright.config.ts``, default ``"e2e"``).
    """
    config = _facade.load_config()
    repos = []
    for name, entry in config.raw.get("e2e_repos", {}).items():
        repos.append(
            E2ERepo(
                name=name,
                url=entry.get("url", ""),
                branch=entry.get("branch", "main"),
                e2e_dir=entry.get("e2e_dir", "e2e"),
            )
        )
    return repos


def _peer_tunnel_fields(entry: object) -> Mapping[str, object]:
    """The tunnel's declared fields — anything but a table of them is malformed."""
    if not isinstance(entry, Mapping):
        message = f"tunnel must be a table of fields, not {type(entry).__name__}"
        raise TypeError(message)
    return {str(key): value for key, value in entry.items()}


def _peer_host(value: object) -> str:
    """The connectable target. Absent, blank or null it renders ``ssh … None``, so it raises."""
    if not isinstance(value, str) or not value:
        message = "tunnel must name the host to connect to"
        raise TypeError(message)
    return value


def _peer_transport(value: object) -> PeerTransport:
    known = ", ".join(sorted(member.value for member in PeerTransport))
    declared = str(value)
    try:
        return PeerTransport(declared)
    except ValueError:
        message = f"unknown tunnel transport {declared!r}; known transports are {known}"
        raise ValueError(message) from None


def _peer_remote_port(value: object) -> int:
    """The far end of the forward, as a port number.

    Substituting the shipped default for an unparsable one RUNS and forwards to the wrong
    port — worse than the unrunnable command the tunnel fields exist to end. ``bool`` is
    rejected ahead of ``int`` because it passes ``isinstance`` and renders as ``:True``.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    message = "tunnel remote_port must be a port number"
    raise TypeError(message)


def _peer_options(value: object) -> tuple[str, ...]:
    """The transport's verbatim extra arguments — anything but a list of them is malformed."""
    if not isinstance(value, list | tuple):
        message = "tunnel options must be a list of arguments"
        raise TypeError(message)
    return tuple(str(option) for option in value)


def _parse_peer_tunnel(peer: str, entry: object) -> PeerTunnel | None:
    """Build the tunnel *peer* declares, or ``None`` when it declares none.

    Every malformed field fails LOUD, named against the peer it came from: the caller surfaces
    the raise as a visible row, whereas coercing a bad field to a shipped default prints — and
    ``t3 peer up`` then RUNS — a command aimed somewhere nobody asked for. Only the peer's own
    label is ever echoed; a resolved host or transport option never is.
    """
    if entry is None:
        return None
    try:
        fields = _peer_tunnel_fields(entry)
        return PeerTunnel(
            host=_peer_host(fields.get("host")),
            transport=_peer_transport(fields.get("transport", PeerTransport.SSH)),
            remote_port=_peer_remote_port(fields.get("remote_port", DEFAULT_REMOTE_PORT)),
            options=_peer_options(fields.get("options", ())),
        )
    except (TypeError, ValueError) as exc:
        message = f"peer {peer!r}: {exc}"
        raise ValueError(message) from None


def load_peer_instances() -> list[PeerInstance]:
    """Load the peer teatree boxes from the ``peer_instances`` registry (DB-home).

    Reads ``config.raw["peer_instances"]`` — which ``load_config`` populates from the
    DB-home ``peer_instances`` ``ConfigSetting`` row (``_inject_db_registries``), exactly
    as :func:`load_e2e_repos` reads its own. Each entry specifies ``url`` (a loopback
    tunnel to that box's dashboard) and optionally ``note`` and ``timeout_seconds``.

    An entry may also declare ``tunnel`` — ``host`` plus optional ``transport``,
    ``remote_port`` and ``options`` — saying how the forward into ``url`` is opened. The key
    it is filed under is the peer's LABEL, which is not a connectable target, so a peer that
    wants its tunnel command shown states the target there rather than being guessed at.
    """
    config = _facade.load_config()
    peers = []
    for name, entry in config.raw.get("peer_instances", {}).items():
        peers.append(
            PeerInstance(
                name=name,
                url=str(entry.get("url", "")),
                note=str(entry.get("note", "")),
                timeout_seconds=float(entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
                tunnel=_parse_peer_tunnel(str(name), entry.get("tunnel")),
            )
        )
    return peers


def clone_root() -> Path:
    """Canonical CLONE root — where main repo clones live (``~/workspace``).

    This is the OLD ``workspace_dir()`` semantics, kept intact for every
    clone-discovery caller (``find_clone_path`` + the direct readers). It is
    DISTINCT from :func:`worktree_root` (the per-overlay WORKTREE root): conflating
    the two would make provisioning scan the worktree root for clones and fail
    with "No git clone found".

    Resolution, first match wins:

    1.  ``T3_WORKSPACE_DIR`` — the env var, then the ``settings.T3_WORKSPACE_DIR``
        Django setting: the explicit, highest-precedence override an operator uses
        to pin clones somewhere other than ``~/workspace``.
    2.  the default ``~/workspace``.

    Pre-Django safe: the env tier short-circuits before any Django access, and the
    Django-settings probe is guarded so a caller with Django unconfigured still
    resolves the default rather than raising.
    """
    env_override = os.environ.get("T3_WORKSPACE_DIR")
    if env_override:
        return Path(env_override).expanduser()
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 — deferred: Django import at call time

    with suppress(ImproperlyConfigured):
        from django.conf import settings  # noqa: PLC0415 — deferred: Django import at call time

        if hasattr(settings, "T3_WORKSPACE_DIR"):
            return Path(settings.T3_WORKSPACE_DIR)
    return Path.home() / "workspace"


def _default_worktree_root(overlay_name: str) -> Path:
    """The sound per-overlay default WORKTREE root: ``~/workspace/t3-workspaces/<overlay>/``.

    Worktrees regroup under a dedicated dir PER OVERLAY so a multi-overlay host
    keeps each overlay's worktrees apart. With no resolvable overlay the base
    ``~/workspace/t3-workspaces`` stands (no overlay subdir).

    This is a PURE resolver — it never touches the filesystem. Directory creation
    happens at the point of USE (ticket-dir provisioning, the relocate move target)
    so the getter has no side effect; the walkers that read it (clean-all's
    ``remove_empty_ticket_dirs``, the landscape survey) each guard a not-yet-created
    dir on their own.
    """
    base = Path.home() / "workspace" / "t3-workspaces"
    return base / overlay_name if overlay_name else base


def worktree_root() -> Path:
    """Canonical per-overlay WORKTREE root (where ticket worktrees are created).

    DISTINCT from :func:`clone_root` (the ``~/workspace`` CLONE root): this names
    where worktrees REGROUP, the clone root names where source clones live.

    Resolution precedence, first match wins:

    1.  ``T3_WORKSPACE_DIR`` — the env var, then the ``settings.T3_WORKSPACE_DIR``
        Django setting: an explicit, highest-precedence override kept for
        back-compat (an operator who pinned a workspace dir keeps it — and it then
        pins BOTH this root and :func:`clone_root`, matching pre-regroup behaviour).
    2.  the DB-home ``ConfigSetting`` ``workspace_dir`` row — the active overlay's
        scope first (a per-overlay opinion), then the global scope (a workspace
        default for every overlay). Set with
        ``t3 <overlay> config_setting set workspace_dir <path> [--overlay <name>]``.
    3.  the sound default ``~/workspace/t3-workspaces/<overlay>/``.

    The active overlay is resolved exactly as every other per-overlay setting
    (``T3_OVERLAY_NAME`` → cwd discovery → the single installed overlay). The DB
    tier is read through the resolution helpers (the deferred loader → resolution
    edge the module docstring describes) so it stays fail-safe to "no row" when
    Django is unconfigured.
    """
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.config.resolution import (  # noqa: PLC0415 — deferred: breaks loader ↔ resolution cycle
        _db_global_overrides,
        _db_overlay_overrides,
        _resolved_overlay_name,
    )

    env_override = os.environ.get("T3_WORKSPACE_DIR")
    if env_override:
        return Path(env_override).expanduser()
    # Guard the settings probe like clone_root(): accessing an attribute on an
    # unconfigured LazySettings raises ImproperlyConfigured, so a pre-Django caller
    # must fall through to the DB / default tiers rather than crash (fail-safe).
    with suppress(ImproperlyConfigured):
        from django.conf import settings  # noqa: PLC0415 — deferred: Django import at call time

        if hasattr(settings, "T3_WORKSPACE_DIR"):
            return Path(settings.T3_WORKSPACE_DIR)

    overlay_name = _resolved_overlay_name(None)
    stored = _db_overlay_overrides(overlay_name).get("workspace_dir")
    if stored is None:
        stored = _db_global_overrides().get("workspace_dir")
    if stored is not None:
        return Path(str(stored)).expanduser()
    return _default_worktree_root(overlay_name)


def check_for_updates(*, force: bool = False) -> str | None:
    """Resolve a "new release available" notice from config + update_check.

    Reads ``check_updates`` (DB-home) from the ``ConfigSetting`` store via the
    Django-free ``cold_reader`` — so the opt-out is honoured on the pre-Django CLI
    paths that are this function's only readers (the root callback, the plain-Typer
    ``t3 config check-update``), with no Django bootstrap. A missing row fails open
    to ``True`` (the dataclass default). Delegates to
    :func:`teatree.update_check.run_update_check` (split out for module-health LOC).
    """
    from teatree.config import cold_reader  # noqa: PLC0415 — Django-free DB read on the pre-Django path

    return run_update_check(check_updates=cold_reader.bool_setting("check_updates", default=True), force=force)
