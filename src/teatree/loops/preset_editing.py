"""Preset ENTRY edits and activation — one write seam for the CLI and the dashboard (#3559).

The tri-state per-loop opinion and the L3 activation live here; the preset
lifecycle (create / rename / delete / metadata) is
:mod:`teatree.loops.preset_admin` and the weekly calendar is
:mod:`teatree.loops.schedule_editing`. All three are the seams the
``t3 loop preset …`` / ``t3 loop schedule …`` commands and the dashboard editor
share, so the two surfaces can never diverge on validation or on what a write means.

The tri-state is the load-bearing part. A preset entry is ``True`` (force on),
``False`` (mask off), or **absent** — and absent is NOT off: it hands the decision
back to the loop's own ``Loop.enabled`` column, now and in future. Setting an
entry to ``inherit`` therefore DELETES the key rather than storing ``False``.
"""

import datetime as dt
from typing import Final

from teatree.core.mode_resolution import clear_mode_override, set_mode_override
from teatree.core.models import Loop, Mode
from teatree.loop.preset_resolution import next_boundary

#: The three values a preset entry can be set to. ``inherit`` removes the key.
ENTRY_ON: Final = "on"
ENTRY_OFF: Final = "off"
ENTRY_INHERIT: Final = "inherit"
ENTRY_STATES: Final[tuple[str, str, str]] = (ENTRY_ON, ENTRY_OFF, ENTRY_INHERIT)

_ENTRY_BOOLS: Final[dict[str, bool]] = {ENTRY_ON: True, ENTRY_OFF: False}


class PresetEditError(ValueError):
    """A preset/schedule write named an unknown target or carried an invalid value."""


def require_preset(name: str) -> Mode:
    """The preset row named *name*, refusing when it does not exist."""
    preset = Mode.objects.by_name(name)
    if preset is None:
        msg = f"no preset named {name!r}"
        raise PresetEditError(msg)
    return preset


def entry_state_of(preset: Mode, loop_name: str) -> str:
    """The tri-state token for *loop_name* — ``on`` / ``off`` / ``inherit`` (absent)."""
    opinion = preset.state_for(loop_name)
    if opinion is None:
        return ENTRY_INHERIT
    return ENTRY_ON if opinion else ENTRY_OFF


def apply_entry_edits(entries: object, edits: list[str]) -> dict[str, bool]:
    """Fold ``inbox=on`` / ``review=off`` / ``dream=inherit`` edits into *entries* (a copy).

    *entries* is the raw stored map (a JSONField value, so ``object``); non-bool
    existing values (a corrupt / legacy row) are dropped, so an edit always produces
    a clean tri-state map.
    """
    updated: dict[str, bool] = (
        {str(key): value for key, value in entries.items() if isinstance(value, bool)}
        if isinstance(entries, dict)
        else {}
    )
    for edit in edits:
        loop_name, _, raw = edit.partition("=")
        name = loop_name.strip()
        value = raw.strip().lower()
        if not name or value not in ENTRY_STATES:
            msg = f"invalid --set {edit!r}; use <loop>=on|off|inherit"
            raise ValueError(msg)
        if value == ENTRY_INHERIT:
            updated.pop(name, None)
        else:
            updated[name] = _ENTRY_BOOLS[value]
    return updated


def set_preset_entry(preset_name: str, loop_name: str, value: str) -> Mode:
    """Set one loop's tri-state opinion on *preset_name* and persist it.

    ``inherit`` removes the key entirely — the preset then holds no opinion and the
    loop's base ``enabled`` column decides.
    """
    preset = require_preset(preset_name)
    state = value.strip().lower()
    if state not in ENTRY_STATES:
        msg = f"invalid entry value {value!r}; use on|off|inherit"
        raise PresetEditError(msg)
    if not Loop.objects.filter(name=loop_name).exists():
        msg = f"no loop named {loop_name!r}"
        raise PresetEditError(msg)
    preset.entries = apply_entry_edits(preset.entries, [f"{loop_name}={state}"])
    preset.save(update_fields=["entries", "updated_at"])
    return preset


def activate_preset(
    name: str,
    *,
    until: dt.datetime | None = None,
    hold: bool = False,
    reason: str = "",
    user_id: str = "",
) -> None:
    """Activate *name* as the L3 manual override through the mode-override chokepoint.

    Without ``hold`` or an explicit ``until`` the override expires at the next
    scheduled boundary, matching ``t3 loop preset use``'s default.
    """
    require_preset(name)
    expiry = None if hold else (until or next_boundary())
    set_mode_override(name, until=expiry, reason=reason, user_id=user_id)


def clear_preset_override(*, user_id: str = "") -> bool:
    """Clear the manual override so the active schedule decides again."""
    return clear_mode_override(user_id=user_id)


__all__ = [
    "ENTRY_INHERIT",
    "ENTRY_OFF",
    "ENTRY_ON",
    "ENTRY_STATES",
    "PresetEditError",
    "activate_preset",
    "apply_entry_edits",
    "clear_preset_override",
    "entry_state_of",
    "require_preset",
    "set_preset_entry",
]
