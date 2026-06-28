"""TeaTree configuration — overlay discovery from ~/.teatree.toml.

The ``teatree.config`` package facade. Config concerns are split by cohesion —
``enums`` (the four config enums), ``settings`` (dataclasses + override
registries), ``loader`` (``load_config`` + the toml/dir entry points),
``discovery`` (overlay discovery), and ``resolution`` (effective-settings +
the per-setting resolvers) — and re-exported here so every ``teatree.config.<name>``
import and ``patch`` target keeps resolving against this stable namespace. The
submodules reach each other's ``load_config`` / ``discover_*`` / ``CONFIG_PATH``
through this facade at call-time, which both breaks the import cycle and keeps a
single ``patch("teatree.config.<name>")`` honoured by every internal caller.
"""

from teatree.config.discovery import (
    _active_overlay_entry,
    _canonical_active_overlay_name,
    _discover_from_manage_py,
    _extract_settings_module,
    _match_canonical_ep,
    _resolve_ep_project_path,
    discover_active_overlay,
    discover_overlays,
)
from teatree.config.enums import AgentRuntime, Autonomy, MissingIssuePolicy, Mode, OnBehalfPostMode, Speed, TeamsDisplay
from teatree.config.homes import DERIVED_FIELDS, SETTING_HOMES, SettingHome
from teatree.config.loader import (
    CONFIG_PATH,
    _load_toml,
    check_for_updates,
    clone_root,
    default_logging,
    load_config,
    load_e2e_repos,
    worktree_root,
    worktrees_dir,
)
from teatree.config.resolution import (
    _active_overlay_overrides,
    _apply_autonomy,
    _global_pinned_fields,
    _overlay_overrides_by_name,
    _overlay_speak_override,
    cadence_seconds,
    get_effective_settings,
)
from teatree.config.settings import (
    BOOTSTRAP_FILE_ONLY_SETTINGS,
    ENV_SETTING_OVERRIDES,
    OVERLAY_OVERRIDABLE_SETTINGS,
    TOML_OVERLAY_OVERRIDABLE_SETTINGS,
    E2ERepo,
    OverlayEntry,
    TeaTreeConfig,
    UserSettings,
    _default_handover_mirror_path,
    _parse_disk_cache_allowlist,
    _parse_env_bool,
    _parse_str_list,
    _parse_user_identity_aliases,
)
from teatree.config_mr_reminder import MrReminderConfig, mr_reminder_from_table, resolve_mr_reminder
from teatree.config_speak import resolve_speak, speak_from_subtable
from teatree.paths import DATA_DIR, get_data_dir

__all__ = [
    "BOOTSTRAP_FILE_ONLY_SETTINGS",
    "CONFIG_PATH",
    "DATA_DIR",
    "DERIVED_FIELDS",
    "ENV_SETTING_OVERRIDES",
    "OVERLAY_OVERRIDABLE_SETTINGS",
    "SETTING_HOMES",
    "TOML_OVERLAY_OVERRIDABLE_SETTINGS",
    "AgentRuntime",
    "Autonomy",
    "E2ERepo",
    "MissingIssuePolicy",
    "Mode",
    "MrReminderConfig",
    "OnBehalfPostMode",
    "OverlayEntry",
    "SettingHome",
    "Speed",
    "TeaTreeConfig",
    "TeamsDisplay",
    "UserSettings",
    "_active_overlay_entry",
    "_active_overlay_overrides",
    "_apply_autonomy",
    "_canonical_active_overlay_name",
    "_default_handover_mirror_path",
    "_discover_from_manage_py",
    "_extract_settings_module",
    "_global_pinned_fields",
    "_load_toml",
    "_match_canonical_ep",
    "_overlay_overrides_by_name",
    "_overlay_speak_override",
    "_parse_disk_cache_allowlist",
    "_parse_env_bool",
    "_parse_str_list",
    "_parse_user_identity_aliases",
    "_resolve_ep_project_path",
    "cadence_seconds",
    "check_for_updates",
    "clone_root",
    "default_logging",
    "discover_active_overlay",
    "discover_overlays",
    "get_data_dir",
    "get_effective_settings",
    "load_config",
    "load_e2e_repos",
    "mr_reminder_from_table",
    "resolve_mr_reminder",
    "resolve_speak",
    "speak_from_subtable",
    "worktree_root",
    "worktrees_dir",
]
