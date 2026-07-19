"""TeaTree configuration — the DB-home config facade + overlay discovery.

The ``teatree.config`` package facade. Config concerns are split by cohesion —
``enums`` (the config enums), ``settings`` (dataclasses + override
registries), ``loader`` (``load_config`` + the logging/dir entry points),
``discovery`` (overlay discovery), and ``resolution`` (effective-settings +
the per-setting resolvers) — and re-exported here so every ``teatree.config.<name>``
import and ``patch`` target keeps resolving against this stable namespace. The
submodules reach each other's ``load_config`` / ``discover_*`` through this facade
at call-time, which both breaks the import cycle and keeps a single
``patch("teatree.config.<name>")`` honoured by every internal caller.
"""

from teatree.config.agent_enums import AgentHarness, AgentHarnessProvider, AgentRuntime, EvalCredential
from teatree.config.cold_hook_settings import COLD_HOOK_SETTINGS, ColdHookSetting
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
from teatree.config.e2e_repo import E2ERepo
from teatree.config.enums import (
    Autonomy,
    CriticGateMode,
    MissingIssuePolicy,
    Mode,
    OnBehalfPostMode,
    SendProxyMode,
    TeamsDisplay,
    Wip,
)
from teatree.config.feature_flags import FEATURE_FLAGS, FeatureFlag, FlagStage, dark_flags, is_feature_flag
from teatree.config.homes import BOOTSTRAP_ENV_ONLY_SETTINGS, DERIVED_FIELDS, SETTING_HOMES, SettingHome
from teatree.config.loader import (
    check_for_updates,
    clone_root,
    default_logging,
    load_config,
    load_e2e_repos,
    worktree_root,
)
from teatree.config.mr_reminder import MrReminderConfig, mr_reminder_from_table, resolve_mr_reminder
from teatree.config.registries import COLD_SETTINGS, REGISTRY_SETTINGS
from teatree.config.resolution import (
    _active_overlay_overrides,
    _apply_autonomy,
    _overlay_overrides_by_name,
    cadence_seconds,
    get_effective_settings,
)
from teatree.config.setting_parsers import (
    _default_handover_mirror_path,
    _parse_disk_cache_allowlist,
    _parse_env_bool,
    _parse_handover_mirror_path,
    _parse_str_list,
    _parse_user_identity_aliases,
)
from teatree.config.settings import (
    ENV_SETTING_OVERRIDES,
    OVERLAY_OVERRIDABLE_SETTINGS,
    SAFETY_POSTURE_KEYS,
    TOML_OVERLAY_OVERRIDABLE_SETTINGS,
    OverlayEntry,
    TeaTreeConfig,
    UserSettings,
)
from teatree.config.speak import resolve_speak, speak_from_subtable
from teatree.config.trusted_authors import effective_trusted_issue_authors
from teatree.paths import DATA_DIR, get_data_dir

__all__ = [
    "BOOTSTRAP_ENV_ONLY_SETTINGS",
    "COLD_HOOK_SETTINGS",
    "COLD_SETTINGS",
    "DATA_DIR",
    "DERIVED_FIELDS",
    "ENV_SETTING_OVERRIDES",
    "FEATURE_FLAGS",
    "OVERLAY_OVERRIDABLE_SETTINGS",
    "REGISTRY_SETTINGS",
    "SAFETY_POSTURE_KEYS",
    "SETTING_HOMES",
    "TOML_OVERLAY_OVERRIDABLE_SETTINGS",
    "AgentHarness",
    "AgentHarnessProvider",
    "AgentRuntime",
    "Autonomy",
    "ColdHookSetting",
    "CriticGateMode",
    "E2ERepo",
    "EvalCredential",
    "FeatureFlag",
    "FlagStage",
    "MissingIssuePolicy",
    "Mode",
    "MrReminderConfig",
    "OnBehalfPostMode",
    "OverlayEntry",
    "SendProxyMode",
    "SettingHome",
    "TeaTreeConfig",
    "TeamsDisplay",
    "UserSettings",
    "Wip",
    "_active_overlay_entry",
    "_active_overlay_overrides",
    "_apply_autonomy",
    "_canonical_active_overlay_name",
    "_default_handover_mirror_path",
    "_discover_from_manage_py",
    "_extract_settings_module",
    "_match_canonical_ep",
    "_overlay_overrides_by_name",
    "_parse_disk_cache_allowlist",
    "_parse_env_bool",
    "_parse_handover_mirror_path",
    "_parse_str_list",
    "_parse_user_identity_aliases",
    "_resolve_ep_project_path",
    "cadence_seconds",
    "check_for_updates",
    "clone_root",
    "dark_flags",
    "default_logging",
    "discover_active_overlay",
    "discover_overlays",
    "effective_trusted_issue_authors",
    "get_data_dir",
    "get_effective_settings",
    "is_feature_flag",
    "load_config",
    "load_e2e_repos",
    "mr_reminder_from_table",
    "resolve_mr_reminder",
    "resolve_speak",
    "speak_from_subtable",
    "worktree_root",
]
