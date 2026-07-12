"""Shared ``[teatree] <flag>`` boolean + integer setting readers for the hook leaves (#2746).

Extracted from ``hook_router`` so a leaf gate (e.g. ``memory_recall``) can read its
own kill-switch WITHOUT importing ``hook_router`` — which would create a
``hook_router`` ↔ leaf import cycle (``hook_router`` imports the leaf's handler into
its ``_HANDLERS`` chain). It imports nothing first-party at load: the DB read
delegates to ``teatree.config.cold_reader`` via a LAZY import, so the leaf stays
import-light for the fast-hook budget.

The readers are DB-only: a gate flag resolves from the canonical ``ConfigSetting``
store via the Django-free ``teatree.config.cold_reader``, else the per-setting
default. There is no config file, so a WRITE that
steers a cold-hook gate targets the DB tier: ``config_setting set`` for the
overridable keys, and ``t3 <overlay> gate <name> disable/enable`` for the cold-hook
gate keys (it writes the canonical DB via
:func:`teatree.config.cold_writer.write_setting`). A missing/unreadable DB row
resolves to the per-setting default, so a gate never silently flips its verdict
(never-lockout).
"""

import os
import sys

# Alias both identities so a bare ``from teatree_settings import ...`` (the live
# hook, whose dir is on sys.path) and ``hooks.scripts.teatree_settings`` (a
# subprocess/test import) resolve the SAME module object.
sys.modules.setdefault("teatree_settings", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.teatree_settings", sys.modules[__name__])

_GLOBAL_SCOPE = ""


def _cold_db_bool(name: str) -> bool | None:
    """The stored GLOBAL-scope DB bool for ``[teatree] <name>``; ``None`` on absence/failure.

    Delegates to the Django-free ``teatree.config.cold_reader``, lazily imported so
    this leaf stays import-light at load. Fails open to ``None`` on ANY error —
    ``teatree`` not importable, an unreadable/locked DB, a missing row, a non-bool
    value — so the caller's per-setting default still stands (never-lockout).
    """
    try:
        from teatree.config.cold_reader import read_setting  # noqa: PLC0415 — deferred: cold-hook import

        value = read_setting(name, scope=_GLOBAL_SCOPE)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return None
    return value if isinstance(value, bool) else None


def section_bool_setting(section: str, name: str, *, default: bool) -> bool:
    """DB-only read of a ``[section] <flag>`` boolean.

    Resolves the gate flag from the DB ``ConfigSetting`` store — ``section``
    ``teatree`` maps to the GLOBAL scope, the only section the cold flags use — else
    *default*. Only a real DB bool is honoured: a quoted ``"false"`` (a str) never
    disables a ``default=True`` flag and a quoted ``"true"`` never enables a
    ``default=False`` one. A missing/unreadable DB row resolves to *default*, so a
    gate never silently changes its verdict (never-lockout). A non-``teatree``
    section has no DB scope and always resolves to *default*.
    """
    if section == "teatree":
        db_value = _cold_db_bool(name)
        if db_value is not None:
            return db_value
    return default


def teatree_bool_setting(name: str, *, default: bool = True) -> bool:
    """Best-effort read of a ``[teatree] <name>`` boolean flag (see :func:`section_bool_setting`)."""
    return section_bool_setting("teatree", name, default=default)


def _cold_db_raw(name: str) -> object | None:
    """The stored GLOBAL-scope DB value for ``[teatree] <name>``, un-coerced.

    Unlike :func:`_cold_db_bool` (which collapses a present-but-non-bool value to
    ``None``), this returns the raw decoded value so a caller can tell a genuinely
    ABSENT setting apart from one whose stored value is not a clean boolean. Fails
    open to ``None`` on any error.
    """
    try:
        from teatree.config.cold_reader import read_setting  # noqa: PLC0415 — deferred: cold-hook import

        return read_setting(name, scope=_GLOBAL_SCOPE)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return None


def teatree_bool_setting_loud(name: str, *, default: bool) -> bool:
    """Read ``[teatree] <name>`` as a boolean, WARNING LOUDLY on an unknown value (#1564).

    A gate toggle must be a clean boolean. When the stored DB value is PRESENT but
    not a bool — a typo like ``"yes"``, ``"on"``, or ``2`` — the sibling readers
    silently fall back to the default, so a mistyped kill-switch fails SILENTLY (the
    operator thinks the gate is off; it is on). This reader instead emits one loud
    stderr line naming the setting and the offending value, then returns *default* —
    the misconfiguration is visible, not swallowed. An ABSENT setting is not
    "unknown" and is silent.
    """
    db_raw = _cold_db_raw(name)
    if db_raw is not None:
        if isinstance(db_raw, bool):
            return db_raw
        _warn_unknown_setting(name, db_raw, default=default)
    return default


def _warn_unknown_setting(name: str, value: object, *, default: bool) -> None:
    sys.stderr.write(
        f"WARNING: [teatree] {name} = {value!r} is not a boolean — expected true/false. "
        f"Falling back to the default ({str(default).lower()}). Fix the value with "
        f"`t3 <overlay> config_setting set {name} <true|false>`.\n"
    )


def _cold_db_int(name: str) -> int | None:
    """The stored GLOBAL-scope DB int for ``[teatree] <name>``; ``None`` on absence/failure.

    The integer sibling of :func:`_cold_db_bool`. Lazily delegates to the Django-free
    ``teatree.config.cold_reader`` and fails open to ``None`` on ANY error. REJECTS a
    ``bool``: a ``bool`` subclasses ``int``, but a stored boolean must never be read
    as a budget, so it returns ``None`` and the caller's default fires instead
    (never-lockout).
    """
    try:
        from teatree.config.cold_reader import read_setting  # noqa: PLC0415 — deferred: cold-hook import

        value = read_setting(name, scope=_GLOBAL_SCOPE)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def section_int_setting(section: str, name: str, *, default: int, minimum: int | None = None) -> int:
    """DB-only read of a ``[section] <name>`` integer budget.

    The integer sibling of :func:`section_bool_setting`. Resolves from the DB
    ``ConfigSetting`` store — ``section`` ``teatree`` maps to the GLOBAL scope, the
    only section the cold budgets use — else *default*. Only a real int (never a
    bool) is honoured. A value below *minimum* is malformed and degrades to *default*
    so the bound it encodes can't be mistyped away (a ``deny_circuit_breaker_threshold``
    of ``0`` never disables the breaker); ``minimum=0`` keeps ``0`` valid so an
    explicit "off" budget survives. A missing/unreadable DB row resolves to *default*.
    A non-``teatree`` section has no DB scope and always resolves to *default*.
    """
    if section == "teatree":
        db_value = _cold_db_int(name)
        if db_value is not None:
            return db_value if minimum is None or db_value >= minimum else default
    return default


def teatree_int_setting(name: str, *, default: int, minimum: int | None = None) -> int:
    """Best-effort read of a ``[teatree] <name>`` integer budget (see :func:`section_int_setting`)."""
    return section_int_setting("teatree", name, default=default, minimum=minimum)


_AUTOLOAD_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def autoload_enabled() -> bool:
    """Whether teatree auto-engages a fresh session (#256). Default OFF, fail-closed.

    The cold-hook reader the SessionStart / UserPromptSubmit hooks consult to decide
    default-off engagement. Env-first (``T3_AUTOLOAD`` truthy), else the DB-home
    ``autoload`` flag read via the Django-free ``_cold_db_bool``. Fails CLOSED (OFF)
    on a missing/broken DB, so a fresh install never auto-engages teatree until the
    owner opts in.
    """
    env = os.environ.get("T3_AUTOLOAD", "").strip().lower()
    if env:
        return env in _AUTOLOAD_TRUTHY
    db_value = _cold_db_bool("autoload")
    return db_value if db_value is not None else False
