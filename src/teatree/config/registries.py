"""DB-home registry + cold-setting config keys (the non-``UserSettings`` DB tier).

Three families of DB-home config that live OUTSIDE the ``UserSettings`` dataclass
partition (``config/homes.py``), each declared beside the ``GROUP_PATH`` it renders
under:

*   :data:`REGISTRY_SETTINGS` — the ``overlays`` definition registry (consumed by
    ``discover_overlays`` and every ``raw["overlays"]`` reader), the ``e2e_repos``
    registry (``load_e2e_repos``) and the ``peer_instances`` registry
    (``load_peer_instances``). Each is stored as ONE JSON-dict ``ConfigSetting``
    row and injected into ``config.raw`` by ``loader._inject_db_registries``, so every
    existing ``config.raw[...]`` reader is untouched.

*   :data:`COLD_SETTINGS` — the customer/brand codename lists, the ``[agent]`` spawn
    tables, and a handful of tunables that the pre-Django hook layer reads DIRECTLY
    from the canonical config DB via ``config.cold_reader.read_setting`` (never
    injected into ``config.raw``, never a ``UserSettings`` field). These carry
    customer codenames; the DB store is PRIVATE to the operator, so they belong in
    the DB exactly like every other setting — the leak surface is the ``export``
    path (``SECRET_SETTINGS`` guards it), not the storage.

*   :data:`COLD_HOOK_SETTINGS` — the hook-leaf gate kill-switches and integer budgets
    the cold layer reads through ``cold_reader`` BEFORE any Django bootstrap. No
    dataclass field either.

All three share ONE entry shape, :class:`ColdHookSetting`, so every key with no
``UserSettings`` field declares its parser AND its shipped default in the same place:
the ``defaults.toml`` renderer reads one declaration rather than three, and
``schema.derive_cold_entries`` can pin both halves of an entry against the model.

``config_setting set`` / ``get`` resolve key-ness through ``known_settings``, which
unions these three with ``OVERLAY_OVERRIDABLE_SETTINGS``, so an admin cannot stash a
row no reader would consult. These keys are deliberately NOT in
``OVERLAY_OVERRIDABLE_SETTINGS`` (the ``UserSettings`` partition), so the resolver's
``_coerce_setting_rows`` ignores them and they never masquerade as a settings field.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from teatree.config.setting_parsers import _parse_str_list, _parse_strict_bool, _parse_strict_int, _parse_strict_str

#: What a cold setting stores — the JSON/TOML shapes its parser accepts and returns.
#: Element types stay open so a default is assignable to the renderer's own stored-value
#: union; ``dict`` and ``list`` are invariant, and no reader consumes the narrower form.
type ColdSettingValue = bool | int | str | list[object] | dict[str, Any]


def _parse_registry_dict(raw: object) -> dict[str, Any]:
    """Validate a registry value is a table and return it (stored verbatim as JSON)."""
    if not isinstance(raw, dict):
        msg = f"Invalid registry value {raw!r}; expected a JSON/TOML table"
        raise TypeError(msg)
    return cast("dict[str, Any]", raw)


@dataclass(frozen=True)
class ColdHookSetting:
    """Contract for a pre-Django, DB-home setting with NO ``UserSettings`` field.

    ``parse`` coerces a stored value exactly as the read-side parser does, and
    ``default`` is what the settings tier serves when no row is stored — the SHIPPED
    default, which ``defaults.toml`` renders for a ``Category.DEFAULT`` key and which
    ``schema.derive_cold_entries`` pins against the model. A consumer may narrow that
    further in its own vocabulary — ``xhigh`` for an empty ``agent_session_effort``,
    ``token-outage`` for an empty ``token_outage_preset_name`` — but that reading belongs
    to the call site, never to this tier. ``scope`` is the GLOBAL store scope every one of these resolves from:
    they are NOT per-overlay overridable.
    """

    parse: Callable[[Any], Any]
    default: ColdSettingValue
    scope: str = ""


#: Where these keys render in the settings hierarchy — declared HERE, beside the keys
#: they group, so adding one below needs no edit in ``setting_groups``.
REGISTRY_SETTINGS_GROUP_PATH: tuple[str, ...] = ("Registries", "Definitions")

#: The registry whose unreadability makes every configured overlay vanish, so a consumer
#: reading an empty overlay set can tell "unconfigured" from "could not be read".
REGISTRY_OVERLAYS: str = "overlays"

REGISTRY_SETTINGS: dict[str, ColdHookSetting] = {
    REGISTRY_OVERLAYS: ColdHookSetting(_parse_registry_dict, default={}),
    "e2e_repos": ColdHookSetting(_parse_registry_dict, default={}),
    "peer_instances": ColdHookSetting(_parse_registry_dict, default={}),
}

REGISTRY_KEYS: tuple[str, ...] = tuple(REGISTRY_SETTINGS)


# The cold-read DB keys: read straight from the canonical config DB by the hook /
# CLI layer via ``cold_reader.read_setting`` (Django-free), so they are set with
# ``config_setting set`` (validated through the parser here) and never touch a file.
COLD_SETTINGS_GROUP_PATH: tuple[str, ...] = ("Registries", "Term scanning, agent tables & cold reads")

COLD_SETTINGS: dict[str, ColdHookSetting] = {
    # Customer / brand / partner codename lists (stored as JSON arrays). The DB is
    # personal, so these are safe here; ``SECRET_SETTINGS`` keeps them out of a
    # shared ``config_setting export``. These four are now LEGACY sources folded into
    # ``banned_term_registry`` (``banned_brands`` → leak, ``banned_terms`` →
    # prose_collider, ``overlay_leak_terms`` → overlay, ``banned_terms_allowlist`` →
    # allow); every gate resolves through the registry via
    # ``banned_term_registry.terms_for_gate`` and only FALLS BACK to these rows when
    # the registry is unset. They stay registered as that fallback tier — do not
    # delete them. An unset list is not an empty ban set: the fail-loud gates raise
    # rather than read this default, which exists as the shipped/export shape.
    "banned_terms": ColdHookSetting(_parse_str_list, default=[]),
    "banned_terms_allowlist": ColdHookSetting(_parse_str_list, default=[]),
    "banned_brands": ColdHookSetting(_parse_str_list, default=[]),
    # The class-tagged registry (`leak`/`prose_collider`/`tone`/`overlay`/`allow` →
    # term lists) the four legacy lists fold into — the single source every
    # term-scanning gate resolves through. A JSON table, so it is validated by the
    # registry-dict parser, not the str-list one.
    "banned_term_registry": ColdHookSetting(_parse_registry_dict, default={}),
    "internal_publish_namespaces": ColdHookSetting(_parse_str_list, default=[]),
    "private_repos": ColdHookSetting(_parse_str_list, default=[]),
    # Legacy overlay-leak fallback (folded into the registry's ``overlay`` class).
    "overlay_leak_terms": ColdHookSetting(_parse_str_list, default=[]),
    # ``[agent]`` spawn tables (str->str/bool/int maps) + scalars, read by the
    # dispatch paths (``config.agent_spawn`` / ``model_tiering``) via ``cold_reader``.
    "agent_phase_models": ColdHookSetting(_parse_registry_dict, default={}),
    "agent_skill_models": ColdHookSetting(_parse_registry_dict, default={}),
    "agent_tier_models": ColdHookSetting(_parse_registry_dict, default={}),
    "agent_pydantic_ai_tier_models": ColdHookSetting(_parse_registry_dict, default={}),
    "agent_tier_effort": ColdHookSetting(_parse_registry_dict, default={}),
    "agent_phase_fanout": ColdHookSetting(_parse_registry_dict, default={}),
    "agent_phase_harness": ColdHookSetting(_parse_registry_dict, default={}),
    # Per-phase history trim depth for the pydantic-ai lane (``lane_b.compaction``).
    "agent_compaction_keep_recent": ColdHookSetting(_parse_registry_dict, default={}),
    # Per-phase depth at which that lane stubs a stale tool result (#4816).
    "agent_compaction_keep_tool_results": ColdHookSetting(_parse_registry_dict, default={}),
    # Per-call Bash output ceiling on the pydantic-ai lane (``lane_b.shell``, #4816).
    "agent_shell_max_output_bytes": ColdHookSetting(_parse_strict_int, default=16384),
    # Per-model price overrides for ``t3 cost`` (model-id substring -> in/out rates).
    "cost_model_prices": ColdHookSetting(_parse_registry_dict, default={}),
    "agent_session_model": ColdHookSetting(_parse_strict_str, default=""),
    "agent_session_effort": ColdHookSetting(_parse_strict_str, default=""),
    "agent_session_permission_mode": ColdHookSetting(_parse_strict_str, default=""),
    "agent_honesty_model": ColdHookSetting(_parse_strict_str, default=""),
    # Tunables that used to live in the file: the timeouts / loops sub-tables, the
    # operator's Slack id, and the master fail-open gate switch (the always-available
    # Bash/gate self-rescue).
    "slack_user_id": ColdHookSetting(_parse_strict_str, default=""),
    "slack_user_channel": ColdHookSetting(_parse_strict_str, default=""),
    "timeouts": ColdHookSetting(_parse_registry_dict, default={}),
    "loops": ColdHookSetting(_parse_registry_dict, default={}),
    "danger_gate_fail_open": ColdHookSetting(_parse_strict_bool, default=False),
    # Loop preset + schedule layer (#3159): the active weekly-schedule selector and
    # the default-off low-token auto-engage flag + its re-pointable target preset.
    "active_loop_schedule": ColdHookSetting(_parse_strict_str, default=""),
    "token_outage_auto_engage": ColdHookSetting(_parse_strict_bool, default=False),
    "token_outage_preset_name": ColdHookSetting(_parse_strict_str, default=""),
    # Host-keyed logins the operator ALSO acts as (``{"gitlab.com": ["my-bot"]}``), read
    # cold by the pre-push foreign-MR guard: a forge CLI answers with ONE login, so an MR
    # our own bot authored is otherwise indistinguishable from a teammate's.
    "self_forge_identities": ColdHookSetting(_parse_registry_dict, default={}),
}

COLD_SETTING_KEYS: tuple[str, ...] = tuple(COLD_SETTINGS)


# Disjoint from ``OVERLAY_OVERRIDABLE_SETTINGS`` and from every ``UserSettings``
# field. These keys are read cold from the canonical config DB (via ``cold_reader``)
# by the pre-Django hook leaves; ``config_setting`` get/set/clear handle them as
# part of the unified known-key set (each is folded into ``_ALLOWED_SETTINGS`` by its
# parser), resolving to this in-code default when no DB row exists. A fitness test
# enumerates the live cold-read sites and asserts every one is registered here, so a
# new hook gate flag added without an entry turns the suite red.
COLD_HOOK_SETTINGS_GROUP_PATH: tuple[str, ...] = ("Gates", "Pre-Django hooks")

COLD_HOOK_SETTINGS: dict[str, ColdHookSetting] = {
    # ``teatree_bool_setting`` gate kill-switches the hook leaves read cold.
    "memory_recall_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "orchestrator_investigation_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "orchestrator_delegation_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "unknown_repo_push_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "no_self_reviewer_assign_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "glab_stale_base_remote_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "git_add_all_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "foreign_branch_push_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "general_purpose_agent_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "merged_detection_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "config_overwrite_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "cron_loop_shell_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "completion_claim_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "headless_authoring_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "main_clone_guard_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "single_branch_repo_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "deny_circuit_breaker_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "skill_loading_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "plan_edit_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "visible_plan_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "mcp_privacy_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "self_dm_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "mcp_slack_write_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "dispatch_quote_gate_on_task_create_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "dispatch_quote_scan_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "banned_terms_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    # Not a gate kill-switch: the banned-terms scanner's UNSET-list posture. False (the
    # dev/solo default) warns and allows; True is the deployment that MUST scrub (#3247).
    "banned_terms_required": ColdHookSetting(_parse_strict_bool, default=False),
    "orchestrator_boundary_agent_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "out_of_band_merge_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "raw_pr_create_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "standing_goal_stop_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "stop_snapshotter_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "answer_first_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "unbacked_claim_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "brief_anchor_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    # Not a kill-switch: the brief-anchor lint's POSTURE. False (the default) warns and
    # allows; True refuses an unanchored dispatch brief outright (#4341).
    "brief_anchor_gate_refuse": ColdHookSetting(_parse_strict_bool, default=False),
    "verbatim_paste_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    # Bespoke integer budgets ``hook_router`` reads straight from ``[teatree]``.
    "orchestrator_turn_budget": ColdHookSetting(_parse_strict_int, default=25),
    "orchestrator_turn_wall_clock_seconds": ColdHookSetting(_parse_strict_int, default=180),
    "hook_validator_timeout_seconds": ColdHookSetting(_parse_strict_int, default=60),
}
