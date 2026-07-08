"""DB-home registry + cold-setting config keys (the non-``UserSettings`` DB tier).

Two families of DB-home config that live OUTSIDE the ``UserSettings`` dataclass
partition (``config/homes.py``):

*   :data:`REGISTRY_SETTINGS` — the ``overlays`` definition registry (consumed by
    ``discover_overlays`` and every ``raw["overlays"]`` reader) and the ``e2e_repos``
    registry (``load_e2e_repos``). Each is stored as ONE JSON-dict ``ConfigSetting``
    row and injected into ``config.raw`` by ``loader._inject_db_registries``, so every
    existing ``config.raw[...]`` reader is untouched.

*   :data:`COLD_SETTINGS` — the customer/brand codename lists, the ``[agent]`` spawn
    tables, and a handful of tunables that the pre-Django hook layer reads DIRECTLY
    from the canonical config DB via ``config.cold_reader.read_setting`` (never
    injected into ``config.raw``, never a ``UserSettings`` field). These carry
    customer codenames; the DB store is PRIVATE to the operator, so they belong in
    the DB exactly like every other setting — the leak surface is the ``export``
    path (``SECRET_SETTINGS`` guards it), not the storage.

``config_setting set`` / ``get`` consult all three registries (this union with
``OVERLAY_OVERRIDABLE_SETTINGS``) to allow + validate a key, so an admin cannot
stash a row no reader would consult. These keys are deliberately NOT in
``OVERLAY_OVERRIDABLE_SETTINGS`` (the ``UserSettings`` partition), so the resolver's
``_coerce_db_rows`` ignores them and they never masquerade as a settings field.
"""

from collections.abc import Callable
from typing import Any, cast

from teatree.config.setting_parsers import _parse_str_list, _parse_strict_bool, _parse_strict_str


def _parse_registry_dict(raw: object) -> dict[str, Any]:
    """Validate a registry value is a table and return it (stored verbatim as JSON)."""
    if not isinstance(raw, dict):
        msg = f"Invalid registry value {raw!r}; expected a JSON/TOML table"
        raise TypeError(msg)
    return cast("dict[str, Any]", raw)


REGISTRY_SETTINGS: dict[str, Callable[[Any], Any]] = {
    "overlays": _parse_registry_dict,
    "e2e_repos": _parse_registry_dict,
}

REGISTRY_KEYS: tuple[str, ...] = tuple(REGISTRY_SETTINGS)


# The cold-read DB keys: read straight from the canonical config DB by the hook /
# CLI layer via ``cold_reader.read_setting`` (Django-free), so they are set with
# ``config_setting set`` (validated through the parser here) and never touch a file.
COLD_SETTINGS: dict[str, Callable[[Any], Any]] = {
    # Customer / brand / partner codename lists (stored as JSON arrays). The DB is
    # personal, so these are safe here; ``SECRET_SETTINGS`` keeps them out of a
    # shared ``config_setting export``.
    "banned_terms": _parse_str_list,
    "banned_terms_allowlist": _parse_str_list,
    "banned_brands": _parse_str_list,
    "internal_publish_namespaces": _parse_str_list,
    "private_repos": _parse_str_list,
    "overlay_leak_terms": _parse_str_list,
    # ``[agent]`` spawn tables (str->str/bool/int maps) + scalars, read by the
    # dispatch paths (``config_agent`` / ``model_tiering``) via ``cold_reader``.
    "agent_phase_models": _parse_registry_dict,
    "agent_skill_models": _parse_registry_dict,
    "agent_tier_models": _parse_registry_dict,
    "agent_pydantic_ai_tier_models": _parse_registry_dict,
    "agent_tier_effort": _parse_registry_dict,
    "agent_phase_fanout": _parse_registry_dict,
    "agent_session_model": _parse_strict_str,
    "agent_session_effort": _parse_strict_str,
    "agent_honesty_model": _parse_strict_str,
    # Tunables that used to live in the file: the E2E private-specs dir, the
    # availability schedule / timeouts / loops sub-tables, the operator's Slack id,
    # and the master fail-open gate switch (the always-available Bash/gate self-rescue).
    "private_tests": _parse_strict_str,
    "slack_user_id": _parse_strict_str,
    "availability_schedule": _parse_registry_dict,
    "timeouts": _parse_registry_dict,
    "loops": _parse_registry_dict,
    "danger_gate_fail_open": _parse_strict_bool,
}

COLD_SETTING_KEYS: tuple[str, ...] = tuple(COLD_SETTINGS)
