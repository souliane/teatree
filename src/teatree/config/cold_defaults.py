"""Django-free, pydantic-free stdlib reader for the shipped ``defaults.toml``.

The reader :func:`teatree.config.resolution._toml_default_rows` layers the DEFAULTS
tier from. It exists as a stdlib reader — rather than reading the same file through
:func:`teatree.config.schema.shipped_defaults` — because ``teatree.config``'s package
init imports ``resolution``, and the cold hook path loads that package init: a
pydantic read there would put the model's ~110ms import on EVERY hook invocation.
Only the standard library is imported — never ``schema``/``pydantic``, never Django,
never a sibling that would pull either — and a subprocess control pins that.

The file is packaged data — hand-editable, and snapshot-able from the live box through
the owner-approved ``manage.py snapshot_settings_defaults``. The ``[teatree]`` table
it exposes carries EXACTLY the ``Category.DEFAULT`` keys (Secret/Personal keys are absent by
construction and have no shipped default).

The FILE nests those keys into the declaration hierarchy as real sub-tables; the KEY
NAMESPACE stays flat, because that namespace is the persisted contract every env
override, ``ConfigSetting`` row and cold sqlite3 read depends on. :func:`flatten_settings_table`
is the one place the two meet, and being the single Django-free reader of the table is
why it lives here. Disambiguation needs no marker: a sub-table whose name is a DECLARED
setting is a genuine nested setting (``speak`` / ``mr_reminder``) and stays a value; any
other sub-table is a group wrapper and is descended into.

The parse is cached per file mtime — an edited ``defaults.toml`` invalidates the cache on
the next read, so a fresh value is never masked by a stale parse.
"""

import threading
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Aliased private so the module's public surface stays exactly ``__all__`` (the
# inverse-drift ratchet in ``tests/config/test_cold_defaults.py``). The four config-key
# registries this unites are pydantic-free and Django-free, and ``teatree.config``'s
# package init already imports them, so the cold path pays nothing for the read.
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS as _DECLARED_SETTING_KEYS

#: The packaged shipped-defaults file — the ONE path every default reader resolves
#: it from: this stdlib one, ``schema.shipped_defaults``, and the resolver's
#: TOML-default tier (``resolution._toml_default_rows``).
DEFAULTS_TOML = Path(__file__).with_name("defaults.toml")
_TEATREE_TABLE = "teatree"

_lock = threading.Lock()
_cache: dict[int, dict[str, Any]] = {}

__all__ = ["DEFAULTS_TOML", "flatten_settings_table", "shipped_defaults_table"]


def flatten_settings_table(table: Mapping[str, Any]) -> dict[str, Any]:
    """Collapse a nested ``[teatree]`` table to the flat key namespace readers depend on.

    A sub-table named after a DECLARED setting is that setting's value and is returned
    whole (``speak``, ``mr_reminder`` — genuine nested settings). Every other sub-table is
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


def shipped_defaults_table(path: Path | None = None) -> dict[str, Any]:
    """The ``[teatree]`` table of ``defaults.toml``, FLAT, parsed once per file mtime.

    The file nests its keys into the declaration hierarchy; :func:`flatten_settings_table`
    collapses that back to the flat namespace every reader below this one expects, so
    nesting is a file shape and never reaches a resolver, an env override or a stored row.

    Returns a fresh copy so a caller can never mutate the cached parse. A missing
    file yields an empty table (a fresh checkout always ships the file; the empty
    fallback keeps a cold read from raising).

    The default path is resolved from :data:`DEFAULTS_TOML` at CALL time, not bound as a
    default argument at import time — a test that re-points the module constant at a
    fixture file would otherwise be silently ignored by every no-argument caller while
    ``resolution._toml_default_rows`` (which passes it explicitly) honoured it, so the key
    set and the values could come from two different files.
    """
    resolved = DEFAULTS_TOML if path is None else path
    try:
        mtime = resolved.stat().st_mtime_ns
    except OSError:
        return {}
    with _lock:
        table = _cache.get(mtime)
        if table is None:
            parsed = tomllib.loads(resolved.read_text(encoding="utf-8")).get(_TEATREE_TABLE, {})
            table = flatten_settings_table(parsed)
            _cache.clear()  # only the current-mtime parse is worth keeping
            _cache[mtime] = table
    return dict(table)
