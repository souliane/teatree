"""TeaTree config loading — ``load_config`` + the toml/logging/dir entry points.

``CONFIG_PATH``, ``load_config`` (builds ``UserSettings`` from ``~/.teatree.toml``),
the toml loader, the default Django LOGGING dict, ``load_e2e_repos``, and the
``workspace_dir`` / ``worktrees_dir`` / ``check_for_updates`` resolvers. Split out
of the package facade for the RUF067 init-is-re-exports-only rule; re-exported
from ``teatree.config`` so every ``teatree.config.<name>`` path stays valid. The
per-setting resolvers live in ``resolution`` and are reached through the package
facade at call-time (the partition's loader -> resolution edge, deferred to avoid
the loader/resolution/discovery import cycle).

``load_config`` builds only the **TOML file tier**, and under the #1775 hard
partition only the TOML-home carve-out reads off it — the global ``[teatree]``
table merged onto the dataclass defaults. Every DB-home field keeps its dataclass
default here; its authoritative value comes from the ``ConfigSetting`` store. The
remaining tiers — env, the DB store (``ConfigSetting`` rows, for DB-home fields),
and the per-overlay ``[overlays.<name>]`` table (for TOML-home fields) — are
layered on top per-field by ``resolution.get_effective_settings`` according to
each field's home; consult its docstring for the per-home precedence. Callers that
need effective values must use ``get_effective_settings``, not the bare
``load_config().user`` (which sees neither env, the DB store, nor per-overlay).
"""

import logging
import os
import tomllib
from contextlib import suppress
from pathlib import Path
from typing import Any

import teatree.config as _facade
from teatree.config.settings import E2ERepo, TeaTreeConfig, UserSettings, _default_handover_mirror_path, _parse_str_list
from teatree.config_mr_reminder import resolve_mr_reminder
from teatree.config_speak import resolve_speak
from teatree.paths import DATA_DIR, get_data_dir
from teatree.update_check import run_update_check

CONFIG_PATH = Path.home() / ".teatree.toml"

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


def _load_toml(path: Path) -> dict:
    """Parse ``path`` as TOML, re-raising a syntax error as a named config error.

    A raw ``tomllib.TOMLDecodeError`` would propagate a parser traceback
    through ``main()`` on every ``t3`` command (even ``--help``); instead it
    becomes a typed, message-bearing ``ValueError`` naming the file and the
    parser's position — the same error shape the intentional invalid-``mode``
    path raises.
    """
    with path.open("rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            msg = f"Malformed TOML in config file {path}: {exc}"
            raise ValueError(msg) from exc


# Sentinel for a TOML value that cannot be coerced to its setting's type. It never
# equals a parsed DB value, so a malformed TOML value still counts as a conflict.
_UNPARSEABLE_TOML_VALUE = object()


def _parse_toml_value(key: str, value: object) -> object:
    """Coerce a raw TOML scalar through the same parser the DB store uses for *key*.

    Both sides of a conflict comparison must be normalized the same way (a TOML
    ``"auto"`` and a stored ``"auto"`` both become ``Mode.AUTO``) so the check
    compares semantic values, not representations. A parse failure means the TOML
    value is malformed for the setting's type — treated as "not equal" so the
    conflict still surfaces rather than being swallowed.
    """
    from teatree.config.settings import OVERLAY_OVERRIDABLE_SETTINGS  # noqa: PLC0415

    parser = OVERLAY_OVERRIDABLE_SETTINGS.get(key)
    if parser is None:
        return value
    try:
        return parser(value)
    except (ValueError, TypeError, AttributeError):
        return _UNPARSEABLE_TOML_VALUE


def _db_home_keys_in(toml_table: dict[str, Any]) -> list[str]:
    """The DB-home keys present in *toml_table* (a cheap, static, no-DB check).

    ``_MIGRATED_SETTING_KEYS`` are excluded — they are warned about unconditionally
    by :func:`_warn_migrated_keys_in_toml`, so routing them through the quiet
    conflict-only path here would double-warn.
    """
    from teatree.config.homes import SETTING_HOMES, SettingHome  # noqa: PLC0415

    return [key for key in toml_table if SETTING_HOMES.get(key) is SettingHome.DB and key not in _MIGRATED_SETTING_KEYS]


# Settings whose ``UserSettings`` field was removed (souliane/teatree#2731). A
# stored ``[teatree]`` / ``[overlays.<name>]`` value for one of these resolves to
# nothing — the key no longer maps to any field — so a leftover entry is warned
# about (never silently no-opped) so the operator knows it has no effect.
_RETIRED_SETTING_KEYS: frozenset[str] = frozenset({"branch_prefix", "ask_before_post_on_behalf"})

# DB-home keys whose SILENT TOML drop changes high-impact behaviour — ``workspace_dir``
# (historically the CLONE root ``~/workspace``) now names the per-overlay WORKTREE
# root, so a leftover ``[teatree] workspace_dir`` silently RELOCATES where worktrees
# are created. Unlike the quiet DB-home conflict path (which warns only when the
# TOML value DISAGREES with a stored row), these warn whenever PRESENT — a silent
# relocation with zero signal is unacceptable. The operator migrates the value into
# the ``ConfigSetting`` store with ``config_setting import`` (or sets it explicitly).
_MIGRATED_SETTING_KEYS: frozenset[str] = frozenset({"workspace_dir"})


def _warn_migrated_keys_in_toml(raw: dict, path: Path) -> None:
    """Emit ONE aggregated WARN for a high-impact migrated DB-home key still in TOML.

    ``workspace_dir`` moved to the DB store and now drives the per-overlay WORKTREE
    root (``config.worktree_root()``); a value left in ``[teatree]`` /
    ``[overlays.<name>]`` is IGNORED on read and would silently change where
    worktrees are created. This warns on PRESENCE (not just on a value conflict)
    so the operator is told to migrate it. Clones are still discovered under the
    CLONE root ``~/workspace`` (``config.clone_root()``, ``T3_WORKSPACE_DIR`` env).
    """
    offenders: list[str] = []
    teatree = raw.get("teatree")
    if isinstance(teatree, dict):
        offenders.extend(f"[teatree] {key}" for key in _MIGRATED_SETTING_KEYS if key in teatree)
    overlays = raw.get("overlays")
    if isinstance(overlays, dict):
        for overlay_name, overlay_cfg in overlays.items():
            if not isinstance(overlay_cfg, dict):
                continue
            offenders.extend(f"[overlays.{overlay_name}] {key}" for key in _MIGRATED_SETTING_KEYS if key in overlay_cfg)
    if offenders:
        _logger.warning(
            "Config keys in %s are DB-home now and IGNORED on read — leaving a stored value would "
            "silently change where worktrees are created (workspace_dir is the per-overlay WORKTREE "
            "root, not the clone root): %s. Migrate them into the ConfigSetting store with "
            "`t3 <overlay> config_setting import`, or set explicitly with "
            "`t3 <overlay> config_setting set workspace_dir <path> [--overlay <name>]`. Clones are "
            "still discovered under ~/workspace (override with the T3_WORKSPACE_DIR env var).",
            path,
            ", ".join(offenders),
        )


def _warn_retired_keys_in_toml(raw: dict, path: Path) -> None:
    """Emit ONE aggregated WARN for retired setting keys still present in TOML.

    A retired key (its ``UserSettings`` field is gone, souliane/teatree#2731) maps
    to no field, so a stored value resolves to nothing. Unlike a migrated DB-home
    key — which the operator moves into the ``ConfigSetting`` store — a retired key
    has no successor and should simply be deleted from the file.
    """
    offenders: list[str] = []
    teatree = raw.get("teatree")
    if isinstance(teatree, dict):
        offenders.extend(f"[teatree] {key}" for key in _RETIRED_SETTING_KEYS if key in teatree)
    overlays = raw.get("overlays")
    if isinstance(overlays, dict):
        for overlay_name, overlay_cfg in overlays.items():
            if not isinstance(overlay_cfg, dict):
                continue
            offenders.extend(f"[overlays.{overlay_name}] {key}" for key in _RETIRED_SETTING_KEYS if key in overlay_cfg)
    if offenders:
        _logger.warning(
            "Retired setting keys in %s have no effect (souliane/teatree#2731 removed the field) "
            "and are IGNORED on read: %s. Delete them from the file.",
            path,
            ", ".join(offenders),
        )


def _conflicting_db_home_keys(
    toml_table: dict[str, Any], db_home_keys: list[str], db_overrides: dict[str, Any]
) -> list[str]:
    """Return the *db_home_keys* whose TOML value CONFLICTS with the DB.

    A conflict is a key present in BOTH the TOML table and the ``ConfigSetting``
    store with DIFFERING (parser-normalized) values — the only case where the
    TOML value is silently ignored *and* disagrees with the setting's real home,
    so the operator is genuinely surprised. A DB-home key absent from the DB store
    (being migrated away), or present but AGREEING, is silent: it resolves to the
    same effective value, so warning about it is the noise this path removes.
    """
    conflicts: list[str] = []
    for key in db_home_keys:
        if key not in db_overrides:
            continue
        if _parse_toml_value(key, toml_table[key]) != db_overrides[key]:
            conflicts.append(key)
    return conflicts


def _warn_db_home_keys_in_toml(raw: dict, path: Path) -> None:
    """Emit ONE aggregated WARN for DB-home keys whose TOML value CONFLICTS with the DB.

    Under the #1775 hard partition a DB-home key in the TOML file is IGNORED on
    read (its home is the ``ConfigSetting`` store). After an install migrates such
    keys into the store the TOML is clean, so warning on every DB-home key that
    *appears* in the file produced ~100 lines of noise per command. The signal that
    actually matters is a CONFLICT: the key is set to a different value in BOTH the
    TOML and the DB, so the silently-ignored TOML value disagrees with what is in
    effect. Those are aggregated into a SINGLE warning naming every offending key
    and the one-time ``config_setting import`` migration path. A DB-home key that is
    absent from the DB, or agrees with it, is silent.

    The home registry and DB readers are imported lazily (the loader -> resolution
    edge the module docstring describes) to avoid the loader/resolution/discovery
    import cycle at module load and to keep the DB read off the hot import path.

    The DB is read ONLY when a table actually carries a DB-home key. The common
    post-migration file (every DB-home key already moved into the store) has none,
    so ``load_config`` touches no connection — keeping it leak-free off the hot path
    rather than opening a stray default-alias connection on every call.
    """
    offenders: list[str] = []

    teatree = raw.get("teatree")
    if isinstance(teatree, dict):
        db_home_keys = _db_home_keys_in(teatree)
        if db_home_keys:
            from teatree.config.resolution import _db_global_overrides  # noqa: PLC0415

            global_db = _db_global_overrides()
            offenders.extend(f"[teatree] {key}" for key in _conflicting_db_home_keys(teatree, db_home_keys, global_db))

    overlays = raw.get("overlays")
    if isinstance(overlays, dict):
        for overlay_name, overlay_cfg in overlays.items():
            if not isinstance(overlay_cfg, dict):
                continue
            db_home_keys = _db_home_keys_in(overlay_cfg)
            if not db_home_keys:
                continue
            from teatree.config.resolution import _db_overlay_overrides  # noqa: PLC0415

            overlay_db = _db_overlay_overrides(overlay_name)
            offenders.extend(
                f"[overlays.{overlay_name}] {key}"
                for key in _conflicting_db_home_keys(overlay_cfg, db_home_keys, overlay_db)
            )

    if offenders:
        _logger.warning(
            "Config keys in %s are DB-home settings (#1775) set to a DIFFERENT value than the "
            "ConfigSetting store, and are IGNORED on read: %s. Resolve the conflict by removing them "
            "from the file (the DB value is authoritative) or migrate once with "
            "`t3 <overlay> config_setting import`.",
            path,
            ", ".join(offenders),
        )


def load_config(path: Path | None = None) -> TeaTreeConfig:
    if path is None:
        path = _facade.CONFIG_PATH
    if not path.is_file():
        return TeaTreeConfig()

    raw = _load_toml(path)
    _warn_db_home_keys_in_toml(raw, path)
    _warn_retired_keys_in_toml(raw, path)
    _warn_migrated_keys_in_toml(raw, path)

    teatree = raw.get("teatree", {})
    # ``workspace_dir`` / ``worktrees_dir`` are DB-home: their ``[teatree]`` value is
    # ignored on read (warned + migrate via ``config_setting import``); the fields keep
    # their dataclass defaults here and ``config.worktree_root()`` / ``worktrees_dir()``
    # resolve them off the store. (Django ``settings.py`` hardcodes ``TIME_ZONE`` and
    # configures ``DATABASES`` without reading ``worktrees_dir`` / ``timezone``, so
    # neither was ever a DB-open bootstrap dep.)

    # The hard partition (#1775): ``load_config`` builds ONLY the TOML-home fields
    # (the carve-out — pre-Django readers, path/infra bootstrap, nested structured
    # tables). Every DB-home field keeps its dataclass default at the file tier; its
    # value comes from the ``ConfigSetting`` store via ``get_effective_settings``.
    # ``on_behalf_post_mode`` is DB-home (so it keeps its default here). A DB-home key
    # left in ``[teatree]`` / ``[overlays.<name>]`` is ignored on read (its home is the
    # DB); migrate it into the store with ``t3 <overlay> config_setting import``.
    user = UserSettings(
        privacy=teatree.get("privacy", ""),
        # Strict bool: only a real TOML boolean ``true`` engages autoload — a
        # quoted ``"true"`` / ``"false"`` string is ignored (matches the
        # cold-read in ``teatree_settings.autoload_enabled``).
        autoload=teatree.get("autoload", False) is True,
        speak=resolve_speak(teatree),
        mr_reminder=resolve_mr_reminder(raw),
        orchestrator_bash_gate_enabled=bool(teatree.get("orchestrator_bash_gate_enabled", True)),
        statusline_chain=_parse_str_list(teatree["statusline_chain"]) if "statusline_chain" in teatree else [],
        handover_mirror_path=(
            Path(str(teatree["handover_mirror_path"])).expanduser()
            if teatree.get("handover_mirror_path")
            else _default_handover_mirror_path()
        ),
    )

    return TeaTreeConfig(user=user, raw=raw)


def load_e2e_repos(path: Path | None = None) -> list[E2ERepo]:
    """Load named E2E repos from ``[e2e_repos.<name>]`` sections in ``~/.teatree.toml``.

    Each entry may specify ``url``, ``branch``, and optionally ``e2e_dir``
    (the subdirectory containing ``playwright.config.ts``, default ``"e2e"``).
    """
    config = _facade.load_config(path)
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
    Django-settings probe is guarded so a caller with Django unconfigured (a cold
    TOML reader) still resolves the default rather than raising.
    """
    env_override = os.environ.get("T3_WORKSPACE_DIR")
    if env_override:
        return Path(env_override).expanduser()
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

    with suppress(ImproperlyConfigured):
        from django.conf import settings  # noqa: PLC0415

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
    from django.conf import settings  # noqa: PLC0415

    from teatree.config.resolution import (  # noqa: PLC0415
        _db_global_overrides,
        _db_overlay_overrides,
        _resolved_overlay_name,
    )

    env_override = os.environ.get("T3_WORKSPACE_DIR")
    if env_override:
        return Path(env_override).expanduser()
    if hasattr(settings, "T3_WORKSPACE_DIR"):
        return Path(settings.T3_WORKSPACE_DIR)

    overlay_name = _resolved_overlay_name(None)
    stored = _db_overlay_overrides(overlay_name).get("workspace_dir")
    if stored is None:
        stored = _db_global_overrides().get("workspace_dir")
    if stored is not None:
        return Path(str(stored)).expanduser()
    return _default_worktree_root(overlay_name)


def worktrees_dir() -> Path:
    """Canonical worktrees directory (where ticket worktrees are created).

    DB-home (#1775): resolves env/Django override first, then the ``ConfigSetting``
    store (overlay scope then global, stored as a path string), then the dataclass
    default — the path-string accessor pattern ``workspace_dir`` /
    ``worktree_root()`` use. Django-side (it reads ``django.conf.settings``), so it
    reads the store directly, no ``cold_reader`` needed.
    """
    from django.conf import settings  # noqa: PLC0415

    from teatree.config.resolution import (  # noqa: PLC0415
        _db_global_overrides,
        _db_overlay_overrides,
        _resolved_overlay_name,
    )

    if hasattr(settings, "T3_WORKTREES_DIR"):
        return Path(settings.T3_WORKTREES_DIR)
    stored = _db_overlay_overrides(_resolved_overlay_name(None)).get("worktrees_dir")
    if stored is None:
        stored = _db_global_overrides().get("worktrees_dir")
    if stored is not None:
        return Path(str(stored)).expanduser()
    return DATA_DIR / "worktrees"


def check_for_updates(*, force: bool = False) -> str | None:
    """Resolve a "new release available" notice from config + update_check.

    Reads ``check_updates`` (DB-home #1775) from the ``ConfigSetting`` store via
    the Django-free ``cold_reader`` — so the opt-out is honoured on the pre-Django
    CLI paths that are this function's only readers (the root callback, the
    plain-Typer ``t3 config check-update``), with no Django bootstrap. A missing
    row fails open to ``True`` (the dataclass default). Delegates to
    :func:`teatree.update_check.run_update_check` (split out for module-health LOC).
    """
    from teatree.config import cold_reader  # noqa: PLC0415 — Django-free DB read on the pre-Django path

    return run_update_check(check_updates=cold_reader.bool_setting("check_updates", default=True), force=force)
