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
the ``defaults.toml`` renderer reads one declaration rather than three.

``config_setting set`` / ``get`` resolve key-ness through ``known_settings``, which
unions these three with ``OVERLAY_OVERRIDABLE_SETTINGS``, so an admin cannot stash a
row no reader would consult. These keys are deliberately NOT in
``OVERLAY_OVERRIDABLE_SETTINGS`` (the ``UserSettings`` partition), so the resolver's
``_coerce_setting_rows`` ignores them and they never masquerade as a settings field.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from teatree.config.agent_enums import AgentHarness
from teatree.config.agent_spawn import parse_phase_harness_entry, parse_skill_models
from teatree.config.declaration import Category
from teatree.config.setting_parsers import (
    _parse_private_repos,
    _parse_strict_bool,
    _parse_strict_int,
    _parse_strict_str,
)

#: What a cold setting stores — the JSON/TOML shapes its parser accepts and returns.
#: Element types stay open so a default is assignable to the renderer's own stored-value
#: union; ``dict`` and ``list`` are invariant, and no reader consumes the narrower form.
type ColdSettingValue = bool | int | str | list[object] | dict[str, Any]

VERIFICATION_PHASES: frozenset[str] = frozenset({"reviewing", "requesting_review", "testing"})


def _parse_registry_dict(raw: object) -> dict[str, Any]:
    """Validate a registry value is a table and return it (stored verbatim as JSON)."""
    if not isinstance(raw, dict):
        msg = f"Invalid registry value {raw!r}; expected a JSON/TOML table"
        raise TypeError(msg)
    return cast("dict[str, Any]", raw)


def _parse_agent_skill_models(raw: object) -> dict[str, list[object]]:
    value = _parse_registry_dict(raw)
    parse_skill_models(value)
    return cast("dict[str, list[object]]", value)


def _parse_agent_phase_harness(raw: object) -> dict[str, Any]:
    value = _parse_registry_dict(raw)
    for phase, harness in value.items():
        parse_phase_harness_entry(str(phase), harness)
    return value


@dataclass(frozen=True)
class ColdHookSetting:
    """Contract for a pre-Django, DB-home setting with NO ``UserSettings`` field.

    ``parse`` coerces a stored value exactly as the read-side parser does, and
    ``default`` is what the settings tier serves when no row is stored — the SHIPPED
    default, which ``defaults.toml`` renders for a ``Category.DEFAULT`` key. A consumer may narrow that
    further in its own vocabulary — ``xhigh`` for an empty ``agent_session_effort``,
    ``token-outage`` for an empty ``token_outage_preset_name`` — but that reading belongs
    to the call site, never to this tier. ``scope`` is the GLOBAL store scope every one of these resolves from:
    they are NOT per-overlay overridable.
    """

    parse: Callable[[Any], Any]
    default: ColdSettingValue
    scope: str = ""
    help: str = ""
    category: Category = Category.DEFAULT
    stored: object = None


#: Where these keys render in the settings hierarchy — declared HERE, beside the keys
#: they group, so adding one below needs no edit in ``setting_groups``.
REGISTRY_SETTINGS_GROUP_PATH: tuple[str, ...] = ("Registries", "Definitions")

#: The registry whose unreadability makes every configured overlay vanish, so a consumer
#: reading an empty overlay set can tell "unconfigured" from "could not be read".
REGISTRY_OVERLAYS: str = "overlays"

REGISTRY_SETTINGS: dict[str, ColdHookSetting] = {
    REGISTRY_OVERLAYS: ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="per-overlay settings, keyed by overlay name",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    "e2e_repos": ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="external repositories containing Playwright test specs and their clone locations",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    "peer_instances": ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="the boxes to compare against — each one's loopback-tunnel url and how to open it",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
}

REGISTRY_KEYS: tuple[str, ...] = tuple(REGISTRY_SETTINGS)


# The cold-read DB keys: read straight from the canonical config DB by the hook /
# CLI layer via ``cold_reader.read_setting`` (Django-free), so they are set with
# ``config_setting set`` (validated through the parser here) and never touch a file.
COLD_SETTINGS_GROUP_PATH: tuple[str, ...] = ("Registries", "Term scanning, agent tables & cold reads")

TERM_REGISTRY: str = "banned_term_registry"

COLD_SETTINGS: dict[str, ColdHookSetting] = {
    # The single classed term registry is private to the operator and withheld
    # from shared exports. Gates read its classes through terms_for_gate.
    TERM_REGISTRY: ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="extra banned-term groups, each name mapped to its own term list",
        stored=dict[str, Any],
        category=Category.SECRET,
    ),
    "private_repos": ColdHookSetting(
        _parse_private_repos,
        default=[],
        help=(
            "host/owner or host/owner/repo; a namespace covers every repo below it, "
            "so use only for wholly private groups; a reachable PUBLIC probe wins"
        ),
        stored=list[str],
        category=Category.SECRET,
    ),
    # ``[agent]`` spawn tables (str->str/bool/int maps) + scalars, read by the
    # dispatch paths (``config.agent_spawn`` / ``model_tiering``) via ``cold_reader``.
    "agent_phase_models": ColdHookSetting(
        _parse_registry_dict,
        default={
            "planning": "frontier",
            "coding": "frontier",
            "debugging": "frontier",
            "reviewing": "frontier",
            "retrospecting": "frontier",
            "testing": "balanced",
            "shipping": "balanced",
            "requesting_review": "cheap",
        },
        help="per-phase model override, keyed by lifecycle phase name",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    "agent_skill_models": ColdHookSetting(
        _parse_agent_skill_models,
        default={},
        help="per-skill model floor, or an ordered route list of {harness, model, provider?, tier?} fallbacks",
        stored=dict[str, list[object]],
        category=Category.PERSONAL,
    ),
    "agent_tier_models": ColdHookSetting(
        _parse_registry_dict,
        default={
            "frontier": "claude-opus-5-5",
            "balanced": "claude-sonnet-5-5",
            "cheap": "claude-sonnet-5-5",
        },
        help="which model each cost tier resolves to",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    "agent_pydantic_ai_tier_models": ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="model per tier for the pydantic-ai lane",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    "agent_tier_effort": ColdHookSetting(
        _parse_registry_dict,
        default={"frontier": "xhigh", "balanced": "high"},
        help="reasoning effort per cost tier",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    "agent_phase_harness": ColdHookSetting(
        _parse_agent_phase_harness,
        default=dict.fromkeys(VERIFICATION_PHASES, AgentHarness.CLAUDE_SDK.value),
        help="per-phase harness override, for a phase that needs a different CLI",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    # Per-phase history trim depth for the pydantic-ai lane (``lane_b.compaction``).
    "agent_compaction_keep_recent": ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="per-phase count of recent messages a compacted history keeps",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    # Per-phase depth at which that lane stubs a stale tool result (#4816).
    "agent_compaction_keep_tool_results": ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="per-phase count of recent messages (~N/2 tool round-trips) left unstubbed",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    # Per-call Bash output ceiling on the pydantic-ai lane (``lane_b.shell``, #4816).
    "agent_shell_max_output_bytes": ColdHookSetting(
        _parse_strict_int,
        default=16384,
        help="per-call ceiling on Bash output the agent lane carries; 0 is uncapped",
        stored=int,
        category=Category.PERSONAL,
    ),
    # Per-model price overrides for ``t3 cost`` (model-id substring -> in/out rates).
    "cost_model_prices": ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="price per model, used to cost a run",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    "agent_session_model": ColdHookSetting(
        _parse_strict_str,
        default="",
        help="model the main orchestrating session runs on",
        stored=str,
        category=Category.PERSONAL,
    ),
    "agent_session_effort": ColdHookSetting(
        _parse_strict_str,
        default="",
        help="reasoning effort the main session runs at",
        stored=str,
        category=Category.PERSONAL,
    ),
    "agent_session_permission_mode": ColdHookSetting(
        _parse_strict_str,
        default="",
        help="permission mode the main session is pinned to; empty keeps the unattended pin",
        stored=str,
        category=Category.PERSONAL,
    ),
    "agent_honesty_model": ColdHookSetting(
        _parse_strict_str,
        default="",
        help="model used for honesty-critical verification, where over-claiming costs most",
        stored=str,
        category=Category.PERSONAL,
    ),
    # Tunables that used to live in the file: the timeouts / loops sub-tables, the
    # operator's Slack id, and the master fail-open gate switch (the always-available
    # Bash/gate self-rescue).
    "slack_user_id": ColdHookSetting(
        _parse_strict_str, default="", help="the operator's Slack user id", stored=str, category=Category.PERSONAL
    ),
    "slack_user_channel": ColdHookSetting(
        _parse_strict_str,
        default="",
        help="Slack channel the operator is DMed on",
        stored=str,
        category=Category.PERSONAL,
    ),
    "timeouts": ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="per-operation timeout overrides, keyed by operation name",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    "loops": ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="per-loop overrides, keyed by loop name",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
    "danger_gate_fail_open": ColdHookSetting(
        _parse_strict_bool,
        default=False,
        help="let the danger gate pass when it cannot evaluate, instead of refusing",
        stored=bool,
    ),
    # Loop preset + schedule layer (#3159): the active weekly-schedule selector and
    # the token-outage auto-engage setting + its re-pointable target preset.
    "active_loop_schedule": ColdHookSetting(
        _parse_strict_str,
        default="",
        help="name of the weekly schedule deciding when the autonomous loops may run",
        stored=str,
    ),
    "token_outage_preset_name": ColdHookSetting(
        _parse_strict_str, default="", help="preset engaged when the token budget parks", stored=str
    ),
    # Host-keyed logins the operator ALSO acts as (``{"gitlab.com": ["my-bot"]}``), read
    # cold by the pre-push foreign-MR guard: a forge CLI answers with ONE login, so an MR
    # our own bot authored is otherwise indistinguishable from a teammate's.
    "self_forge_identities": ColdHookSetting(
        _parse_registry_dict,
        default={},
        help="per forge host, the logins we also act as — our own bots, never a teammate",
        stored=dict[str, Any],
        category=Category.PERSONAL,
    ),
}

COLD_SETTING_KEYS: tuple[str, ...] = tuple(COLD_SETTINGS)


def tier_models() -> dict[str, str]:
    return dict(cast("dict[str, str]", COLD_SETTINGS["agent_tier_models"].default))


def pydantic_ai_tier_models() -> dict[str, str]:
    return dict(cast("dict[str, str]", COLD_SETTINGS["agent_pydantic_ai_tier_models"].default))


def tier_effort() -> dict[str, str]:
    return dict(cast("dict[str, str]", COLD_SETTINGS["agent_tier_effort"].default))


def phase_models() -> dict[str, str]:
    return dict(cast("dict[str, str]", COLD_SETTINGS["agent_phase_models"].default))


def phase_harness_defaults() -> dict[str, str]:
    return dict(cast("dict[str, str]", COLD_SETTINGS["agent_phase_harness"].default))


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
    "orchestrator_delegation_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse an unbounded orchestrator read that belongs in a sub-agent",
        stored=bool,
    ),
    "unknown_repo_push_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse a push to a repo teatree does not recognise", stored=bool
    ),
    "no_self_reviewer_assign_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse assigning the author as their own reviewer", stored=bool
    ),
    "glab_stale_base_remote_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a merge-request create on a stale glab-base remote (glab no-ops)",
        stored=bool,
    ),
    "git_add_all_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse a whole-tree `git add -A` / `git add .` stage", stored=bool
    ),
    "foreign_branch_push_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse a push onto a branch another author owns", stored=bool
    ),
    "general_purpose_agent_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a blank general-purpose sub-agent for managed-repo work",
        stored=bool,
    ),
    "config_overwrite_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a blind overwrite of a config file that was not read first",
        stored=bool,
    ),
    "cron_loop_shell_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a harness cron that shells a t3 loop command the worker drives",
        stored=bool,
    ),
    "completion_claim_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a multi-deliverable completion claim carrying no evidence map",
        stored=bool,
    ),
    "headless_authoring_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse headless authoring outside a live worktree", stored=bool
    ),
    "main_clone_guard_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse an edit to the main clone's working tree", stored=bool
    ),
    "worktree_containment_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse a linked worktree checkout outside allowed roots", stored=bool
    ),
    "single_branch_repo_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse a second branch in a repo declared single-branch", stored=bool
    ),
    "skill_loading_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse a turn that skipped a skill the loader required", stored=bool
    ),
    "plan_edit_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a source edit made while the session is still planning",
        stored=bool,
    ),
    "visible_plan_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse an action until a plan for every named ticket is in visible assistant text",
        stored=bool,
    ),
    "mcp_slack_write_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="gate MCP Slack writes behind the on-behalf rules", stored=bool
    ),
    "self_dm_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse Slack MCP writes to the operator's own DM under the user token",
        stored=bool,
    ),
    "banned_terms_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="run the banned-term scan before a publish", stored=bool
    ),
    "orchestrator_boundary_agent_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse orchestrator work that belongs in a sub-agent", stored=bool
    ),
    "out_of_band_merge_gate_enabled": ColdHookSetting(
        _parse_strict_bool, default=True, help="refuse a merge that skipped the recorded review path", stored=bool
    ),
    "owner_question_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse batched or shorthand owner questions and decisions left in reply prose",
        stored=bool,
    ),
    "raw_pr_create_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a raw MR/PR create that would open it under the wrong identity",
        stored=bool,
    ),
    "standing_goal_stop_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse ending a turn as done while a standing goal is unmet",
        stored=bool,
    ),
    "standing_grant_ask_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse asking the owner to sign off a merge a standing grant already covers",
        stored=bool,
    ),
    "answer_first_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a turn that reports a dispatch instead of answering the user's question",
        stored=bool,
    ),
    "unbacked_claim_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a diagnosis or an escalation that cites no artefact the agent read",
        stored=bool,
    ),
    "brief_anchor_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a sub-agent brief that asserts specifics it anchors to nothing",
        stored=bool,
    ),
    "verbatim_paste_gate_enabled": ColdHookSetting(
        _parse_strict_bool,
        default=True,
        help="refuse a publish whose body reproduces the operator's own words verbatim",
        stored=bool,
    ),
    # Bespoke integer budgets ``hook_router`` reads straight from ``[teatree]``.
    "orchestrator_turn_budget": ColdHookSetting(
        _parse_strict_int, default=25, help="output-token budget for one orchestrator turn", stored=int
    ),
    "orchestrator_turn_wall_clock_seconds": ColdHookSetting(
        _parse_strict_int, default=180, help="seconds one orchestrator turn may take", stored=int
    ),
    "hook_validator_timeout_seconds": ColdHookSetting(
        _parse_strict_int, default=60, help="seconds a hook validator may run before it is abandoned", stored=int
    ),
}
