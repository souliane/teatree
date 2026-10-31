"""The per-overlay / env override registries for ``UserSettings``.

The three registries that say WHERE a setting's value may come from and HOW a
stored value is coerced — the DB-home parser registry
(``OVERLAY_OVERRIDABLE_SETTINGS``), the ``T3_*`` env overrides
(``ENV_SETTING_OVERRIDES``), and the safety-posture refusal set
(``SAFETY_POSTURE_KEYS``). Split from the sibling ``settings`` module, which
declares the dataclasses themselves; re-exported from ``teatree.config`` so every
``teatree.config.<name>`` path stays valid.
"""

import os
from collections.abc import Callable
from typing import Any, Final

from teatree.config.agent_enums import AgentHarnessProvider, parse_harness_name
from teatree.config.declaration import Bounds, declared_fields
from teatree.config.enums import Mode, Wip
from teatree.config.setting_parsers import _parse_env_bool, _parse_env_str_list, _parse_strict_int
from teatree.config.settings import UserSettings

OVERLAY_OVERRIDABLE_SETTINGS: dict[str, Callable[[Any], Any]] = {
    key: row.parse for key, row in declared_fields(UserSettings).items()
}


# TOML-home keys that ALSO support a per-overlay ``[overlays.<name>]`` override.
# DB-home cutover emptied this: the per-overlay override of a setting now
# lives entirely in the DB (an overlay-scoped ``ConfigSetting`` row); every field is
# DB-home. Discovery still unions this with the DB-home registry; with it empty the union is
# just the DB-home registry.
TOML_OVERLAY_OVERRIDABLE_SETTINGS: dict[str, Callable[[Any], Any]] = {}

# ``T3_*`` env vars that win over both the per-overlay override and the
# global setting. Mapped to ``(UserSettings field, parser)``.
BOX_GLOBAL_SETTINGS: frozenset[str] = frozenset(
    key for key, row in declared_fields(UserSettings).items() if row.box_global
)
SETTING_BOUNDS: dict[str, Bounds] = {
    key: row.bounds for key, row in declared_fields(UserSettings).items() if row.bounds is not None
}


ENV_SETTING_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "T3_MODE": ("mode", Mode.parse),
    "T3_WIP": ("wip", Wip.parse),
    "T3_WRITE_WIP": ("write_wip", int),
    "T3_MERGE_WIP": ("merge_wip", int),
    "T3_AGENT_HARNESS": ("agent_harness", parse_harness_name),
    "T3_AGENT_HARNESS_PROVIDER": ("agent_harness_provider", AgentHarnessProvider.parse),
    "T3_OPENAI_COMPATIBLE_BASE_URL": ("openai_compatible_base_url", str),
    "T3_OPENAI_COMPATIBLE_MODEL": ("openai_compatible_model", str),
    "T3_OPENAI_COMPATIBLE_LANE": ("openai_compatible_lane", str),
    "T3_ON_BEHALF_AUTO_ACTIONS": ("on_behalf_auto_actions", _parse_env_str_list),
    "T3_REVIEW_SKILL": ("review_skill", str),
    # #3895 shipped these two master gates ON, so each needs the same one-command
    # kill switch its sibling loop gates already had — an operator stopping a
    # default-ON loop cannot be made to write a DB row first.
    "T3_TRUSTED_ISSUE_AUTHORS": ("trusted_issue_authors", _parse_env_str_list),
    "T3_DREAM_MEMORY_PROMOTE": ("dream_memory_promote", _parse_env_bool),
    "T3_BOOST_CONCURRENCY": ("boost_concurrency", _parse_strict_int),
    "T3_WORKER_QUIESCING": ("worker_quiescing", _parse_env_bool),
    "T3_CONTRIBUTE": ("contribute_plugin_dir", _parse_env_bool),
    "T3_AUTOLOAD": ("autoload", _parse_env_bool),
}


# The ``UserSettings`` fields whose WRITE is itself an authorization / delegation /
# fail-closed-boundary act — not a tunable knob. Writing one of these does not merely
# CONFIGURE a gate: it grants authority (``substrate_auto_merge_authorized_by`` — "the
# config write IS the human authorization"), delegates a keystone sign-off
# (``substrate_self_signoff``), disarms an egress/on-behalf pre-gate
# (a permitting posture, ``on_behalf_auto_actions``), or WIDENS a
# fail-closed intake / egress / maker≠checker allowlist, or configures the
# regulated model set
# (``trusted_issue_authors``, ``send_proxy_allowlist``, ``regulated_path_model_allowlist``,
# ``independent_reviewer_identities``), raises the global
# autonomy posture (``autonomy``), or relaxes an
# autonomous-close boundary (``bulk_close_threshold``). The MCP ``config_setting_set``
# surface REFUSES every key here by declared EFFECT (``teatree.mcp.write_tools`` reads
# this set), so a shell-denied MCP agent can never self-grant merge delegation or widen
# the fail-closed intake allowlist by classifying keys via a name-glob that misses them
# (F9.1). This is EFFECT-based, not name-shaped: the companion conformance test
# (``tests/teatree_mcp/test_write_tools_refusals.py``) walks every ``UserSettings`` field
# and fails CLOSED if a delegation/allowlist/authorization-shaped field is in neither this
# set nor the explicit reviewed ``teatree.mcp.write_tools.MCP_SETTABLE_OK`` allowlist — so
# a future safety-posture field can never ship silently MCP-settable.
#: Which ``T3_*`` var carries each env-overridable setting — ``ENV_SETTING_OVERRIDES`` read
#: the other way round, for the surfaces that start from a setting name.
ENV_VAR_BY_SETTING: Final[dict[str, str]] = {
    field_name: env_var for env_var, (field_name, _parser) in ENV_SETTING_OVERRIDES.items()
}


def env_pinned_value(env_var: str) -> str | None:
    """What *env_var* pins in THIS process, or ``None`` when it pins nothing.

    An exported-but-EMPTY var pins only what its parser can represent. ``T3_FOO=`` is how a
    shell neutralises an inherited pin, and reading it as a bool resolved every
    ``T3_DREAM_*`` phase to ``False`` and stopped the memory phases running — but for a list
    the empty string IS the value, the empty allowlist. The parser answers which, so the two
    cases cannot drift apart the way a second registry of "empty means unset" keys would.

    Only the EMPTY case consults the parser: a non-empty value is handed on unexamined, so a
    typo in a nonempty override still raises where the operator can see it
    rather than falling silently through to the tier below.

    The resolver and :func:`env_pin` share this one predicate so a surface can never report
    a pin the resolver does not apply.
    """
    raw = os.environ.get(env_var)
    if raw is None or raw.strip():
        return raw
    _field, parser = ENV_SETTING_OVERRIDES.get(env_var, ("", None))
    if parser is None:
        return None
    try:
        parser(raw)
    except ValueError:
        return None
    return raw


def env_pin(key: str) -> str:
    """The ``T3_*`` var pinning *key* in THIS process, or ``""`` when none is.

    The env tier outranks every stored tier, so while such a var carries a value a DB write
    to *key* lands in a layer nothing reads back: the write reports success and changes
    nothing an operator can observe. Every surface that offers or accepts an edit asks here.
    """
    env_var = ENV_VAR_BY_SETTING.get(key, "")
    return env_var if env_var and env_pinned_value(env_var) is not None else ""


SAFETY_POSTURE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "autonomy",
        "regulated_path_model_allowlist",
        "substrate_self_signoff",
        "substrate_auto_merge_authorized_by",
        "on_behalf_auto_actions",
        "send_proxy_allowlist",
        "trusted_issue_authors",
        "user_identity_aliases",
        "independent_reviewer_identities",
        "bulk_close_threshold",
        "eval_ci_account_allowlist",
        "agent_route_paid_fallback",
    }
)
