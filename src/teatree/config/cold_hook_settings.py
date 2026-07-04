"""The pre-Django cold-hook import registry (config-unify PR2).

Split out of ``settings.py`` (the module-health LOC cap) — these keys are a
distinct concern from the ``UserSettings`` dataclass and its override registries:
they have NO dataclass field at all. Each is a hook-leaf gate flag or integer
budget the cold layer reads straight from ``~/.teatree.toml`` BEFORE any Django
bootstrap. Because the TOML->DB import only walked ``OVERLAY_OVERRIDABLE_SETTINGS``,
these keys were silently dropped on import, so a disabled gate or a raised
threshold would reset to its in-code default the moment a later PR flips the cold
reader onto the DB store. ``COLD_HOOK_SETTINGS`` closes that gap.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from teatree.config.settings import _parse_strict_bool, _parse_strict_int


@dataclass(frozen=True)
class ColdHookSetting:
    """Import contract for a pre-Django, hook-only global ``[teatree]`` setting.

    ``parse`` coerces a stored value exactly as the read-side parser does, and
    ``default`` pins the cold reader's in-code fallback (asserted equal to the
    hook's own default by the no-silent-drop fitness test). ``scope`` is the
    GLOBAL store scope every one of these resolves from — they are NOT per-overlay
    overridable, so the import writes them only from the global ``[teatree]`` table.
    """

    parse: Callable[[Any], Any]
    default: bool | int
    scope: str = ""


# Disjoint from ``OVERLAY_OVERRIDABLE_SETTINGS`` and from every ``UserSettings``
# field. The TOML->DB import unions this with the overridable registry for the
# global ``[teatree]`` table so a non-default value survives the cutover.
# ``config_setting set`` still refuses these keys (they are not in the overridable
# registry) and the cold readers still hit TOML this PR — the import is purely
# additive, seeding the DB ahead of the reader flip. A fitness test enumerates the
# live cold-read sites and asserts every one is registered here, so a new hook gate
# flag added without an entry turns the suite red.
COLD_HOOK_SETTINGS: dict[str, ColdHookSetting] = {
    # ``teatree_bool_setting`` gate kill-switches the hook leaves read cold.
    "memory_recall_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "orchestrator_investigation_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "unknown_repo_push_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "no_self_reviewer_assign_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "config_overwrite_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "completion_claim_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "main_clone_guard_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "deny_circuit_breaker_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "loop_registration_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "skill_loading_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "plan_edit_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "mcp_privacy_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "self_dm_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "mcp_slack_write_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "dispatch_quote_gate_on_task_create_enabled": ColdHookSetting(_parse_strict_bool, default=False),
    "banned_terms_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    "orchestrator_boundary_agent_gate_enabled": ColdHookSetting(_parse_strict_bool, default=True),
    # Bespoke integer budgets ``hook_router`` reads straight from ``[teatree]``.
    "deny_circuit_breaker_threshold": ColdHookSetting(_parse_strict_int, default=3),
    "orchestrator_turn_budget": ColdHookSetting(_parse_strict_int, default=25),
    "orchestrator_turn_wall_clock_seconds": ColdHookSetting(_parse_strict_int, default=180),
}
