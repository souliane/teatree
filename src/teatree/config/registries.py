"""DB-home registry config (#1775 eliminate-~/.teatree.toml).

The NON-``UserSettings`` config read directly off ``config.raw`` rather than through
the ``UserSettings`` partition: the ``overlays`` definition registry (consumed by
``discover_overlays`` and every ``raw["overlays"]`` reader) and the ``e2e_repos``
registry (``load_e2e_repos``). Each is stored as ONE JSON-dict ``ConfigSetting`` row
and injected into ``raw`` by ``loader._inject_db_registries``, so every existing reader
is untouched. ``config_setting set`` / ``get`` / ``import`` consult ``REGISTRY_SETTINGS``
to allow + validate these keys — they are deliberately NOT in
``OVERLAY_OVERRIDABLE_SETTINGS`` (the ``UserSettings`` partition), so the resolver's
``_coerce_db_rows`` ignores them and they never masquerade as a settings field.
"""

from collections.abc import Callable
from typing import Any, cast


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
