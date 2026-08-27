"""Which TOML table holds what — the layout the export writes and the import reads back.

The third rule the two directions must agree on by construction, beside ``secret_guard``
(what must never be shared) and ``registry_rows`` (how one compound row survives being
described incompletely). Stated once here because a table name or a nesting the export
emitted and the import did not recognise would silently drop rows on the way home.

``sorted_table`` is the one renderer every emitted table goes through, and
``import_candidates`` is its inverse over a parsed document. The seed families own the
same question for their own tables, in :mod:`teatree.core.config_interchange.seed_tables`.
"""

from typing import Any

import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.registries import REGISTRY_KEYS
from teatree.core.config_interchange.registry_rows import overlay_table_split
from teatree.core.models.config_setting import ConfigValue

GLOBAL_SCOPE = ""
TEATREE_TABLE = "teatree"
OVERLAYS_TABLE = "overlays"
E2E_REPOS_TABLE = "e2e_repos"

#: Every registry key EXCEPT ``overlays``, each rendered as its own ``[<key>.<name>]`` tables.
#: Derived from ``REGISTRY_KEYS`` rather than listed, so a registry key added to the schema
#: rides the interchange from that edit alone — a hand-kept list here silently DROPPED it from
#: every export (the key is excluded from ``[teatree]`` and nothing else claimed it).
#: ``overlays`` is the one exception, because its tables merge the registry definitions with
#: each overlay's own setting-scope rows rather than dumping the registry entry alone.
TABLE_REGISTRY_KEYS: tuple[str, ...] = tuple(key for key in REGISTRY_KEYS if key != OVERLAYS_TABLE)


def sorted_table(rows: dict[str, ConfigValue]) -> tomlkit_items.Table:
    """A ``[table]`` of *rows* (key-sorted), each native value rendered as its TOML scalar.

    Sorted so the dump is a deterministic function of the store's CONTENT, not the DB
    insertion order — the property ``export -> import -> export`` byte-stability rests on.
    """
    table = tomlkit.table()
    for key in sorted(rows):
        table[key] = rows[key]
    return table


def registry_value(global_rows: dict[str, ConfigValue], key: str) -> dict[str, Any]:
    """The stored registry dict for *key* in the global rows, or ``{}`` when absent/malformed."""
    value = global_rows.get(key)
    return value if isinstance(value, dict) else {}


def import_candidates(doc: dict[str, Any]) -> list[tuple[str, str, ConfigValue]]:
    """Flatten a parsed export document into ``(scope, key, value)`` candidate rows.

    Reverses the export layout: the ``[teatree]`` table -> global settings; each
    ``[overlays.<name>]`` table splits — through the export's own join predicate,
    :func:`~teatree.core.config_interchange.registry_rows.overlay_table_split` — into per-overlay
    SETTING rows and overlay-DEFINITION keys (``path`` / ``class`` / …, folded back into
    the ``overlays`` registry row); every other registry key's ``[<key>.<name>]`` tables
    rebuild that key's registry row.

    A rebuilt registry value is a candidate, not the row: it describes only what the file
    could say, and the import MERGES it onto the stored row rather than replacing it (see
    :mod:`teatree.core.config_interchange.registry_rows`).

    The ``[teatree]`` table goes through the SAME flattener the cold reader applies, so a
    nested file and a flat one import to the same rows and the group wrappers never reach
    the store as keys.
    """
    candidates: list[tuple[str, str, ConfigValue]] = []
    for key, value in flatten_settings_table(doc.get(TEATREE_TABLE, {})).items():
        candidates.append((GLOBAL_SCOPE, key, value))
    overlays_registry: dict[str, Any] = {}
    for name, table in doc.get(OVERLAYS_TABLE, {}).items():
        if not isinstance(table, dict):
            continue
        settings, definitions = overlay_table_split(table)
        candidates.extend((name, key, value) for key, value in settings.items())
        if definitions:
            overlays_registry[name] = definitions
    if overlays_registry:
        candidates.append((GLOBAL_SCOPE, OVERLAYS_TABLE, overlays_registry))
    for key in TABLE_REGISTRY_KEYS:
        registry: ConfigValue = {
            name: dict(table) for name, table in doc.get(key, {}).items() if isinstance(table, dict)
        }
        if registry:
            candidates.append((GLOBAL_SCOPE, key, registry))
    return candidates


__all__ = [
    "E2E_REPOS_TABLE",
    "GLOBAL_SCOPE",
    "OVERLAYS_TABLE",
    "TABLE_REGISTRY_KEYS",
    "TEATREE_TABLE",
    "import_candidates",
    "registry_value",
    "sorted_table",
]
