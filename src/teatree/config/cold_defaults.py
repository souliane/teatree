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

The parse is cached per file mtime — an edited ``defaults.toml`` invalidates the cache on
the next read, so a fresh value is never masked by a stale parse.
"""

import threading
import tomllib
from pathlib import Path
from typing import Any

#: The packaged shipped-defaults file — the ONE path every default reader resolves
#: it from: this stdlib one, ``schema.shipped_defaults``, and the resolver's
#: TOML-default tier (``resolution._toml_default_rows``).
DEFAULTS_TOML = Path(__file__).with_name("defaults.toml")
_TEATREE_TABLE = "teatree"

_lock = threading.Lock()
_cache: dict[int, dict[str, Any]] = {}

__all__ = ["DEFAULTS_TOML", "shipped_defaults_table"]


def shipped_defaults_table(path: Path | None = None) -> dict[str, Any]:
    """The ``[teatree]`` table of ``defaults.toml``, parsed once per file mtime.

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
            table = tomllib.loads(resolved.read_text(encoding="utf-8")).get(_TEATREE_TABLE, {})
            _cache.clear()  # only the current-mtime parse is worth keeping
            _cache[mtime] = table
    return dict(table)
