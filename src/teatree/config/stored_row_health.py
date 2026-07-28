"""What a stored ``ConfigSetting`` row IS, when no live setting declaration owns it.

souliane/teatree#3862: a row survives the removal of the key it was written under,
and the resolver drops such a row in silence. Rendered bare on an operator surface
it is indistinguishable from a live control — a stored
``issue_implementer_require_label = True`` was read as an intake gate while
``decide_intake`` admits a trusted author with no label at all. So every surface
that lists raw rows appends :func:`stored_row_note`, and a dead key can never read
as a live one.

"Not in the known-key set" is NOT the same as "dead", which is why this is a
classifier rather than a membership test. Three kinds of row live outside the set
and each needs its own answer:

*   a RENAMED key's value still resolves, onto the replacement field, so it is told
    where its value goes rather than reported out of effect;
*   an :data:`INTERNAL_STATE_KEYS` row is live runtime state a module reads and
    writes, deliberately kept in ``ConfigSetting`` rather than its own table — the
    "clear this" remedy would destroy working state, so it is never offered;
*   everything else is an orphan: the removal was never recorded in
    ``RETIRED_SETTINGS``, so the row is named unknown and the remedy is offered.
"""

import dataclasses

from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.retired_settings import CLEAR_REMEDY, RENAMED_SETTING_KEYS, removed_setting


@dataclasses.dataclass(frozen=True, slots=True)
class InternalStateKey:
    """A ``ConfigSetting`` row that is runtime STATE, not an operator-facing setting.

    *owner* is a dotted module path held as a STRING: the config layer sits below
    every module that keeps state this way, so importing one would be a backwards
    edge. ``tests/teatree_config/test_stored_row_health.py`` reads the named module
    and fails if the key no longer appears in it, so the carve-out cannot rot into
    the dead config it exists to prevent.
    """

    key: str
    owner: str
    purpose: str


INTERNAL_STATE_KEYS: tuple[InternalStateKey, ...] = (
    InternalStateKey(
        key="loop_preset_transition_stamp",
        owner="teatree.loops.preset_transitions",
        purpose="the last-applied mode name each transition pass compares against",
    ),
)

_INTERNAL_STATE_BY_KEY: dict[str, InternalStateKey] = {entry.key: entry for entry in INTERNAL_STATE_KEYS}


def internal_state_key(key: str) -> InternalStateKey | None:
    """The internal-state record for *key*, or ``None`` when it is not one."""
    return _INTERNAL_STATE_BY_KEY.get(key)


def stored_row_note(key: str) -> str:
    """What *key* is, when it is not a live setting — the empty string when it is one.

    The remedy is offered only where clearing the row is the right move: a retired or
    orphaned key. Naming a live internal-state row "clearable" would hand the operator
    a destructive instruction — clearing the mode stamp makes the next transition pass
    read a switch that never happened.
    """
    if key in ALL_KNOWN_CONFIG_SETTINGS:
        return ""
    state = internal_state_key(key)
    if state is not None:
        return f"[internal state — {state.purpose}, written by {state.owner}]"
    replacement = RENAMED_SETTING_KEYS.get(key)
    if replacement is not None:
        return f"[retired alias — resolves onto {replacement}]"
    remedy = CLEAR_REMEDY.format(key=key)
    if removed_setting(key) is not None:
        return f"[retired — not in effect; clear with `{remedy}`]"
    return f"[unknown — no live consumer; clear with `{remedy}`]"


__all__ = ["INTERNAL_STATE_KEYS", "InternalStateKey", "internal_state_key", "stored_row_note"]
