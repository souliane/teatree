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

Two shapes are refused outright (#4188), both judged on the RESULTING mask so a row
written before this guard cannot be extended by an unrelated edit:

- Quieting a loop in :data:`teatree.loops.mode_shape.LOAD_BEARING_LOOPS`. Those are what
    free disk and RAM when the box is under pressure, so a mode that stops them removes the
    only mechanism that can recover the machine — from inside the machine. The token-budget
    mode (:func:`teatree.core.models.loop_preset.low_power_preset_name`) is the single
    exception.
- Admitting :data:`teatree.loops.mode_shape.BACKUP_LOOP` while every
    :data:`teatree.loops.mode_shape.DISK_RECLAIM_LOOPS` loop is quiet — the box then keeps
    writing backups with nothing left that can free the space. No preset is exempt from
    this one, the low-power mode included, because keeping the reclaim pair up is exactly
    what that mode already does.

Because both surfaces fold their edits here, neither can write either shape.
"""

import datetime as dt
from collections.abc import Mapping
from typing import Final

from teatree.core.mode_resolution import clear_mode_override, set_mode_override
from teatree.core.models import Loop, Mode
from teatree.core.models.loop_preset import low_power_preset_name
from teatree.loop.preset_resolution import next_boundary
from teatree.loops.mode_shape import BACKUP_LOOP, DISK_RECLAIM_LOOPS, backup_without_reclaim, quieted_load_bearing

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


def refuse_quieted_load_bearing(preset_name: str, entries: Mapping[str, object]) -> None:
    """Refuse a mask that quiets the load-bearing tier or keeps the backup writing unrelieved.

    The whole RESULTING mask is judged rather than the edit alone, so a row written before
    this guard cannot be carried forward by an unrelated edit. The low-power escape exempts
    only the first shape — it never masks the reclaim pair off, so the second never applies
    to it anyway.
    """
    escape = low_power_preset_name()
    quieted = () if preset_name == escape else quieted_load_bearing(entries)
    if quieted:
        msg = (
            f"preset {preset_name!r} may not mask load-bearing loop(s) off: {', '.join(quieted)}. "
            "They are what keeps the box alive and reachable under pressure, so a mode that stops them "
            f"leaves nothing that can recover the machine and no way in to do it by hand. Only {escape!r} "
            "may quiet them."
        )
        raise PresetEditError(msg)
    base_enabled = dict(Loop.objects.filter(name__in=(*DISK_RECLAIM_LOOPS, BACKUP_LOOP)).values_list("name", "enabled"))
    consuming = backup_without_reclaim(entries, base_enabled=base_enabled)
    if consuming is not None:
        msg = f"preset {preset_name!r} {consuming.detail}"
        raise PresetEditError(msg)


def apply_entry_edits(entries: object, edits: list[str], *, preset_name: str) -> dict[str, bool]:
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
    refuse_quieted_load_bearing(preset_name, updated)
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
    preset.entries = apply_entry_edits(preset.entries, [f"{loop_name}={state}"], preset_name=preset.name)
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
    "refuse_quieted_load_bearing",
    "require_preset",
    "set_preset_entry",
]
