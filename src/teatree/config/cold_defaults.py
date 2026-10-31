"""Cold-safe flattening for the generated settings table and TOML interchange."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Aliased private so the module's public surface stays exactly ``__all__`` (the
# inverse-drift ratchet in ``tests/config/test_cold_defaults.py``). The four config-key
# registries this unites are pydantic-free and Django-free, and ``teatree.config``'s
# package init already imports them, so the cold path pays nothing for the read.
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS as _DECLARED_SETTING_KEYS

#: The packaged shipped-defaults file — the ONE path every reader of it resolves it
#: from: this stdlib one and ``schema.shipped_defaults``. It is no longer a resolution
#: tier: ``config/declared_defaults.py`` RENDERS it from the declarations instead.
DEFAULTS_TOML = Path(__file__).with_name("defaults.toml")
__all__ = ["DEFAULTS_TOML", "flatten_settings_table"]


def flatten_settings_table(table: Mapping[str, Any]) -> dict[str, Any]:
    """Collapse a nested ``[teatree]`` table to the flat key namespace readers depend on.

    A sub-table named after a DECLARED setting is that setting's value and is returned
    whole (``mr_reminder`` — a genuine nested setting). Every other sub-table is
    a grouping wrapper the file uses to render the declaration hierarchy, so it is
    descended into and contributes its own keys to the flat result.

    Total over any shape: an already-flat table is returned unchanged, so a hand-written
    file, an older export and the nested shipped file all read to the same mapping.
    """
    flat: dict[str, Any] = {}
    for key, value in table.items():
        if isinstance(value, dict) and key not in _DECLARED_SETTING_KEYS:
            flat.update(flatten_settings_table(value))
        else:
            flat[key] = value
    return flat
