"""Django-free, pydantic-free stdlib reader for the shipped seed tables in ``defaults.toml``.

The default loops, modes and schedules are shipped defaults an operator tunes to kickstart
a box — the same kind of thing a ``[teatree]`` key is — so they ship in the SAME packaged
file, each family under its own top-level table (``[loops.<name>]`` / ``[modes.<name>]`` /
``[schedules.<name>]``). ``teatree.loops.seed`` and ``teatree.loops.preset_seed`` build
their spec dataclasses from these tables; nothing here knows about the ORM.

Only the standard library is imported — never ``schema``/``pydantic``, never Django — so a
cold surface can read a seed table as cheaply as it reads the defaults table, and a
subprocess control pins that. The parse is cached per file mtime, so an edited file
invalidates the cache on the next read.

Sibling of :mod:`teatree.config.cold_defaults`, which owns the ``[teatree]`` table the
resolver's DEFAULTS tier reads. Two readers, one file, one packaged path.
"""

import datetime as dt
import threading
import tomllib
from pathlib import Path
from typing import Any

from teatree.config.cold_defaults import DEFAULTS_TOML

#: A value a seed entry can carry — the TOML shapes ``defaults.toml`` round-trips.
type SeedValue = bool | int | float | str | dt.time | list[object] | dict[str, object]

#: The seed families the shipped file carries, one top-level table each.
SEED_TABLES: tuple[str, ...] = ("loops", "modes", "schedules")

#: The singular family word each seed table is spoken as, and the table it ships in. The
#: ``preset``/``modes`` pair is why this mapping exists rather than an ``f"{family}s"``: the
#: file table is ``[modes.*]``, the model is ``Mode``, and every surface says "preset".
SEED_FAMILIES: dict[str, str] = {"loop": "loops", "preset": "modes", "schedule": "schedules"}

#: Per family, the seed fields that live ON the object's own row: the model attribute each
#: maps onto and the type it must be. This is the interchange surface — what
#: ``config_setting export`` dumps and ``import`` writes back, so an operator's tuned box
#: round-trips through the same command their settings do.
#:
#: :data:`SHIPPED_ONLY_FIELDS` is the deliberate complement: a schedule's ``slots`` are
#: child rows with their own editor (and the seed never re-materialises them for an
#: existing schedule), and a loop's ``prompt_body`` seeds a separate ``Prompt`` row. Both
#: are tuned in ``defaults.toml`` alone, never through the row interchange.
SEED_ROW_FIELDS: dict[str, dict[str, tuple[str, type]]] = {
    "loops": {
        "delay_seconds": ("delay_seconds", int),
        "daily_at": ("daily_at", dt.time),
        "colleague_facing": ("colleague_facing", bool),
        "default_enabled": ("enabled", bool),
        "description": ("description", str),
    },
    "modes": {
        "description": ("description", str),
        "entries": ("entries", dict),
        "defers_questions": ("defers_questions", bool),
        "pauses_self_pump": ("pauses_self_pump", bool),
        "presence_sensitive": ("presence_sensitive", bool),
    },
    "schedules": {
        "description": ("description", str),
        "timezone": ("timezone", str),
    },
}

#: Seed fields the shipped file carries that the row interchange deliberately excludes.
SHIPPED_ONLY_FIELDS: dict[str, tuple[str, ...]] = {"loops": ("prompt_body",), "schedules": ("slots",)}

_lock = threading.Lock()
_cache: dict[tuple[Path, int], dict[str, Any]] = {}

__all__ = [
    "DEFAULTS_TOML",
    "SEED_FAMILIES",
    "SEED_ROW_FIELDS",
    "SEED_TABLES",
    "SHIPPED_ONLY_FIELDS",
    "classify_seed_field",
    "is_shipped",
    "reset_seed_defaults_cache",
    "seed_divergences",
    "shipped_description",
    "shipped_seed_table",
]


def reset_seed_defaults_cache() -> None:
    """Drop the parsed-document memo — the conftest autouse reset (TSH-2/TSH-7)."""
    with _lock:
        _cache.clear()


def shipped_seed_table(table: str, path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The named seed table of ``defaults.toml`` as ``{entry name: entry body}``.

    Returns fresh copies so a caller can never mutate the cached parse. A missing file or
    an absent table yields an empty mapping, and an entry that is not a sub-table is
    skipped — a malformed file degrades to "nothing shipped", never to a raise on a cold
    read. The default path is resolved from :data:`DEFAULTS_TOML` at CALL time so a test
    that re-points the module constant is honoured by no-argument callers too.
    """
    entries = _document(DEFAULTS_TOML if path is None else path).get(table, {})
    if not isinstance(entries, dict):
        return {}
    return {name: dict(body) for name, body in entries.items() if isinstance(body, dict)}


def is_shipped(family: str, name: str, path: Path | None = None) -> bool:
    """Whether *name* is declared in *family*'s shipped seed table (#3842).

    Lives here rather than beside the delete policy because it is a pure question about
    the shipped file, and its lowest consumer is the Django admin — a ``domain``-layer
    module that may not reach up into ``teatree.loops``.

    Families never leak into each other: a loop named ``review`` does not make a preset
    named ``review`` shipped, so each lookup is scoped to its own table.
    """
    return name in shipped_seed_table(SEED_FAMILIES[family], path)


def shipped_description(family: str, name: str, path: Path | None = None) -> str:
    """The shipped one-line description of what *name* does, straight out of the seed table."""
    entry = shipped_seed_table(SEED_FAMILIES[family], path).get(name, {})
    description = entry.get("description", "")
    return str(description) if description else f"the shipped {family} {name!r}"


def _document(path: Path) -> dict[str, Any]:
    """The whole parsed file, memoised per ``(path, mtime)``; a missing file reads as empty.

    The path belongs in the key: an mtime-only key lets a fixture's parse answer for the
    shipped file whenever the two share an mtime — which a coarse-granularity filesystem,
    or a test pinning ``os.utime``, makes real rather than theoretical.
    """
    try:
        key = (path, path.stat().st_mtime_ns)
    except OSError:
        return {}
    with _lock:
        document = _cache.get(key)
        if document is None:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
            _cache.clear()  # only the current parse is worth keeping
            _cache[key] = document
    return document


def seed_divergences(table: str, live: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The live rows' interchange fields that DIFFER from what the file ships.

    The inverse of the settings export: a ``ConfigSetting`` row exists only where an
    operator moved a value off its default, so the seed export likewise carries only the
    fields a live loop / mode / schedule was tuned away from its shipped seed. A row the
    operator never touched contributes nothing, and re-importing the shipped file itself
    therefore writes nothing.
    """
    shipped = shipped_seed_table(table)
    diverged = {}
    for name, fields in live.items():
        entry = shipped.get(name, {})
        diff = {field: value for field, value in fields.items() if entry.get(field) != value}
        if diff:
            diverged[name] = diff
    return diverged


def classify_seed_field(table: str, name: str, field: str, value: SeedValue) -> tuple[str, str]:
    """One imported seed field's disposition: ``("reject", reason)`` / ``("skip"|"write", "")``.

    A name the shipped file does not carry, a field outside the interchange, and a value of
    the wrong type are all rejected — so a seed table is understood per-table rather than
    silently ignored, and one bad entry refuses the whole import. A value equal to what the
    file ships is redundant (``skip``), which is what makes ``defaults.toml`` itself import
    to zero writes.
    """
    entry = shipped_seed_table(table).get(name)
    if entry is None:
        return ("reject", f"unknown {table} entry")
    spec = SEED_ROW_FIELDS[table].get(field)
    if spec is None:
        if field not in SHIPPED_ONLY_FIELDS.get(table, ()):
            return ("reject", "unknown field")
        # A shipped-only field has no row to write onto, so re-importing the value the file
        # already ships is a no-op — and moving it means editing the file itself.
        if entry.get(field) == value:
            return ("skip", "")
        return ("reject", "shipped-only field — tune it in config/defaults.toml")
    if not _is_of_type(value, spec[1]):
        return ("reject", f"invalid: expected {spec[1].__name__}")
    return ("skip" if entry.get(field) == value else "write", "")


def _is_of_type(value: SeedValue, expected: type) -> bool:
    """Type check that keeps ``bool`` out of ``int`` (``isinstance(True, int)`` is True)."""
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)
