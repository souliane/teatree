"""Shared ``[teatree] <flag>`` boolean + integer setting readers for the hook leaves (#2746).

Extracted from ``hook_router`` so a leaf gate (e.g. ``memory_recall``) can read its
own kill-switch WITHOUT importing ``hook_router`` — which would create a
``hook_router`` ↔ leaf import cycle (``hook_router`` imports the leaf's handler into
its ``_HANDLERS`` chain). It imports nothing first-party at load: the DB read
delegates to ``teatree.config.cold_reader`` via a LAZY import (config-unify PR3), so
the leaf stays import-light for the fast-hook budget.

The readers are now DB-first (config-unify PR3): a gate flag resolves from the
canonical ``ConfigSetting`` store — seeded from ``~/.teatree.toml`` by ``t3 setup``
(:func:`teatree.core.config_migration.import_toml_into_db`) — falling back to the
``[teatree] <flag>`` TOML value, then the per-setting default (the #938 dual-read).
Because this reader is DB-first, every WRITE that must steer a cold-hook gate also
targets the DB tier: ``config_setting set`` for the overridable keys, and ``t3
<overlay> gate <name> disable/enable`` for the cold-hook gate keys (it writes the
canonical DB via :func:`teatree.config.cold_writer.write_setting`, falling back to TOML
only in the pre-``t3 setup`` cold state). So a ``t3 teatree gate`` toggle stays authoritative
over a seeded row instead of being shadowed by it. The TOML fallback preserves a value
the import never seeds — critically the never-seeded TOML-home key ``autoload`` (read by
the cold pre-Django hooks via tomllib, #256) — so a missing/unreadable DB row never
silently flips a gate's verdict. (``orchestrator_bash_gate_enabled`` is DB-home as of the
eliminate-~/.teatree.toml cutover and IS seeded by ``import_toml_into_db``, so it is no
longer in this never-seeded set.)
"""

import os
import sys
import tomllib
from pathlib import Path

# Alias both identities so a bare ``from teatree_settings import ...`` (the live
# hook, whose dir is on sys.path) and ``hooks.scripts.teatree_settings`` (a
# subprocess/test import) resolve the SAME module object.
sys.modules.setdefault("teatree_settings", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.teatree_settings", sys.modules[__name__])

_GLOBAL_SCOPE = ""


def _load_home_toml() -> dict:
    """Best-effort parse of ``~/.teatree.toml``; ``{}`` on a missing/broken config."""
    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return {}
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return {}
    return config if isinstance(config, dict) else {}


def _cold_db_bool(name: str) -> bool | None:
    """The stored GLOBAL-scope DB bool for ``[teatree] <name>``; ``None`` on absence/failure.

    Delegates to the Django-free ``teatree.config.cold_reader`` (config-unify PR3),
    lazily imported so this leaf stays import-light at load. Fails open to ``None``
    on ANY error — ``teatree`` not importable, an unreadable/locked DB, a missing
    row, a non-bool value — so the caller's TOML fallback and per-setting default
    still stand (never-lockout).
    """
    try:
        from teatree.config.cold_reader import read_setting  # noqa: PLC0415

        value = read_setting(name, scope=_GLOBAL_SCOPE)
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, bool) else None


def section_bool_setting(section: str, name: str, *, default: bool) -> bool:
    """DB-first, TOML-fallback read of a ``[section] <flag>`` boolean (config-unify PR3).

    Resolves the gate flag from the DB ``ConfigSetting`` store FIRST — ``section``
    ``teatree`` maps to the GLOBAL scope, the only section the cold flags use —
    falling back to the ``[section] <flag>`` TOML value, then *default*. Only a real
    DB bool or a bare TOML boolean is honoured: a quoted ``"false"`` (a str) never
    disables a ``default=True`` flag and a quoted ``"true"`` never enables a
    ``default=False`` one. A missing/unreadable DB row falls through to the SAME
    TOML-or-default verdict the reader produced before the flip, so a gate never
    silently changes its verdict (the #938 dual-read).
    """
    if section == "teatree":
        db_value = _cold_db_bool(name)
        if db_value is not None:
            return db_value
    table = _load_home_toml().get(section)
    if not isinstance(table, dict):
        return default
    value = table.get(name)
    return value if isinstance(value, bool) else default


def teatree_bool_setting(name: str, *, default: bool = True) -> bool:
    """Best-effort read of a ``[teatree] <name>`` boolean flag (see :func:`section_bool_setting`)."""
    return section_bool_setting("teatree", name, default=default)


def _cold_db_int(name: str) -> int | None:
    """The stored GLOBAL-scope DB int for ``[teatree] <name>``; ``None`` on absence/failure.

    The integer sibling of :func:`_cold_db_bool` (config-unify PR4). Lazily delegates
    to the Django-free ``teatree.config.cold_reader`` and fails open to ``None`` on
    ANY error. REJECTS a ``bool``: a ``bool`` subclasses ``int``, but a stored boolean
    must never be read as a budget, so it returns ``None`` and the caller's TOML
    fallback / default fires instead (never-lockout).
    """
    try:
        from teatree.config.cold_reader import read_setting  # noqa: PLC0415

        value = read_setting(name, scope=_GLOBAL_SCOPE)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def section_int_setting(section: str, name: str, *, default: int, minimum: int | None = None) -> int:
    """DB-first, TOML-fallback read of a ``[section] <name>`` integer budget (config-unify PR4).

    The integer sibling of :func:`section_bool_setting`. Resolves from the DB
    ``ConfigSetting`` store FIRST — ``section`` ``teatree`` maps to the GLOBAL scope,
    the only section the cold budgets use — falling back to the ``[section] <name>``
    TOML value, then *default*. Only a real int (never a bool) is honoured at either
    tier. A value below *minimum* is malformed and degrades to *default* so the bound
    it encodes can't be mistyped away (a ``deny_circuit_breaker_threshold`` of ``0``
    never disables the breaker); ``minimum=0`` keeps ``0`` valid so an explicit "off"
    budget survives. A missing/unreadable DB row falls through to the SAME
    TOML-or-default verdict the inline ``tomllib`` reader produced before the flip.
    """
    if section == "teatree":
        db_value = _cold_db_int(name)
        if db_value is not None:
            return db_value if minimum is None or db_value >= minimum else default
    table = _load_home_toml().get(section)
    if isinstance(table, dict):
        value = table.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value if minimum is None or value >= minimum else default
    return default


def teatree_int_setting(name: str, *, default: int, minimum: int | None = None) -> int:
    """Best-effort read of a ``[teatree] <name>`` integer budget (see :func:`section_int_setting`)."""
    return section_int_setting("teatree", name, default=default, minimum=minimum)


_AUTOLOAD_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def autoload_enabled() -> bool:
    """Whether teatree auto-engages a fresh session (#256). Default OFF, fail-closed.

    The cold-hook reader the SessionStart / UserPromptSubmit hooks consult to decide
    default-off engagement. Env-first (``T3_AUTOLOAD`` truthy), else the DB-first
    ``[teatree] autoload`` flag via :func:`teatree_bool_setting`. ``autoload`` is
    TOML-home (#1775) so the import never seeds it — the TOML fallback inside
    :func:`section_bool_setting` is what preserves a configured ``autoload = true``.
    Fails CLOSED (OFF) on a missing/broken config + DB, so a fresh install never
    auto-engages teatree until the owner opts in.
    """
    env = os.environ.get("T3_AUTOLOAD", "").strip().lower()
    if env:
        return env in _AUTOLOAD_TRUTHY
    return teatree_bool_setting("autoload", default=False)
