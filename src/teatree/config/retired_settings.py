"""What a retired DB-home settings key resolves to, and what the operator is told.

souliane/teatree#3527: a retired key is invisible to the resolver — ``_coerce_setting_rows``
drops any row whose key is not in the DB-home parser registry — so removing a
setting silently reverted an operator's explicitly-configured value to the
dataclass default. The removal of ``eval_credential`` reverted the configured
credential operator with nothing said.

The retirements themselves are recorded in
:mod:`teatree.config.retired_settings_ledger`; this module derives the registries the
resolver reads and the sentences the operator is shown. A retirement admits exactly two
outcomes, both of them visible:

*   ``replacement`` set — the key was RENAMED. Its stored value MIGRATES: the row
    resolves onto the replacement field (the canonical key still wins when both
    rows exist), so the operator's opinion survives untouched.
*   ``replacement`` ``None`` — the key was REMOVED. Resolving a stored row emits a
    loud stderr line naming the key, why it went, and the remedy, then falls
    through to the default. Loud rather than fatal is deliberate: a stale row must
    never lock an operator out of their own factory (the never-lockout doctrine),
    but it must never be silent either.
"""

import sys

from teatree.config.retired_settings_ledger import RETIRED_SETTINGS, RetiredSetting

#: The one remedy sentence a surface offers for a stale row — shared so the loud
#: resolver warning and the ``config_setting list`` marker never drift apart.
CLEAR_REMEDY = "t3 <overlay> config_setting clear {key}"


#: Retired key -> the live field its stored value migrates onto.
RENAMED_SETTING_KEYS: dict[str, str] = {
    entry.key: entry.replacement for entry in RETIRED_SETTINGS if entry.replacement is not None
}

#: Retired keys with no replacement — a stored row is reported loudly.
REMOVED_SETTING_KEYS: frozenset[str] = frozenset(entry.key for entry in RETIRED_SETTINGS if entry.replacement is None)

#: Subsystems a retirement removed outright — no eval scenario may still grade one.
RETIRED_SUBSYSTEMS: frozenset[str] = frozenset(
    entry.subsystem for entry in RETIRED_SETTINGS if entry.subsystem is not None
)

_BY_KEY: dict[str, RetiredSetting] = {entry.key: entry for entry in RETIRED_SETTINGS}


def removed_setting(key: str) -> RetiredSetting | None:
    """The removal record for *key*, or ``None`` when it is live or merely renamed."""
    entry = _BY_KEY.get(key)
    return entry if entry is not None and entry.replacement is None else None


def retirement_notice(key: str) -> str | None:
    """What became of *key*, or ``None`` when no retirement is recorded for it.

    souliane/teatree#4094: ``config_setting get``/``set`` answered a retired key with
    the unknown-key refusal, which reads exactly like the answer for a typo — so a
    reader who knows the setting used to exist concludes the mechanism was lost
    rather than superseded. The two outcomes must not read alike either: a rename
    still has an answer to give (the replacement), while a removal has none, and
    saying so is the whole point of recording the reason.
    """
    entry = _BY_KEY.get(key)
    if entry is None:
        return None
    if entry.replacement is not None:
        return (
            f"{key!r} was renamed to {entry.replacement!r} — {entry.reason}. "
            f"Read and write {entry.replacement!r} instead."
        )
    return (
        f"{key!r} was removed — {entry.reason}. It has no replacement. "
        f"Clear any stale row with `{CLEAR_REMEDY.format(key=key)}`."
    )


def warn_removed_setting(entry: RetiredSetting) -> None:
    """Report a stored row under a removed key on stderr — the anti-silent-revert line.

    Named, reasoned, and actionable: without all three the operator learns only
    that something changed. Emitted per resolution rather than once per process so
    it cannot be lost to a warm import in a long-lived loop worker.
    """
    sys.stderr.write(
        f"WARNING: the config setting {entry.key!r} was removed — {entry.reason}. "
        f"Its stored value is NOT in effect and this setting has reverted to its default. "
        f"Clear the stale row with `{CLEAR_REMEDY.format(key=entry.key)}`.\n"
    )
