"""Preset ENTRY edits and activation — one write seam for the CLI and the dashboard (#3559).

The tri-state per-loop opinion and the L3 activation live here; the preset
lifecycle (create / rename / delete / metadata) is
:mod:`teatree.loops.preset_admin` and the weekly calendar is
:mod:`teatree.loops.schedule_editing`. All three are the seams the
``t3 loop preset …`` / ``t3 loop schedule …`` commands and the dashboard editor
share, so the two surfaces can never diverge on validation or on what a write means.

A preset entry is ``True`` (runs) or ``False`` (does not), for EVERY live loop —
there is no absent tier to inherit from (B1). Every edit is folded through
:func:`teatree.core.models.preset_totality.totalized_entries`, so a row an older teatree left
partial is repaired by the next write rather than locking the operator out of it.

One shape is refused outright, judged on the RESULTING mask so a row written before the
guard cannot be extended into it by an unrelated edit: admitting
:data:`teatree.loops.mode_shape.BACKUP_LOOP` while every
:data:`teatree.loops.mode_shape.DISK_RECLAIM_LOOPS` loop is quiet — the box then keeps
writing backups with nothing left that can free the space. No preset is exempt, because
keeping the reclaim pair up while the writer runs is not a posture, it is arithmetic.

Because both surfaces fold their edits here, neither can write that shape.
"""

import datetime as dt
from collections.abc import Mapping
from typing import Final

from django.db import transaction

from teatree.core.mode_resolution import clear_mode_override, set_mode_override
from teatree.core.models import Mode
from teatree.core.models.preset_totality import live_loop_names, totalized_entries
from teatree.loops.mode_shape import backup_without_reclaim

#: The two values a preset entry can be set to.
ENTRY_ON: Final = "on"
ENTRY_OFF: Final = "off"
ENTRY_STATES: Final[tuple[str, str]] = (ENTRY_ON, ENTRY_OFF)

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
    """The token for *loop_name* — ``on`` / ``off``."""
    return ENTRY_ON if preset.state_for(loop_name) else ENTRY_OFF


def refuse_unrelieved_backup(preset_name: str, entries: Mapping[str, object]) -> None:
    """Refuse a mask that keeps the backup writing with both reclaim loops quiet.

    The whole RESULTING mask is judged rather than the edit alone, so a row written before
    this guard cannot be carried forward into the shape by an unrelated edit.
    """
    consuming = backup_without_reclaim(entries)
    if consuming is not None:
        msg = f"preset {preset_name!r} {consuming.detail}"
        raise PresetEditError(msg)


def apply_entry_edits(entries: object, edits: list[str], *, preset_name: str) -> dict[str, bool]:
    """Fold ``inbox=on`` / ``review=off`` edits into *entries*, totalized over every live loop."""
    names = live_loop_names()
    updated = totalized_entries(entries, loop_names=names)
    for edit in edits:
        loop_name, _, raw = edit.partition("=")
        name = loop_name.strip()
        value = raw.strip().lower()
        if not name or value not in ENTRY_STATES:
            msg = f"invalid --set {edit!r}; use <loop>=on|off"
            raise ValueError(msg)
        if name not in names:
            msg = f"no loop named {name!r}"
            raise PresetEditError(msg)
        updated[name] = _ENTRY_BOOLS[value]
    refuse_unrelieved_backup(preset_name, updated)
    return updated


def set_preset_entry(preset_name: str, loop_name: str, value: str) -> Mode:
    """Set one loop's opinion on *preset_name* and persist it.

    ``entries`` is ONE JSON map holding every loop's opinion, so a bare
    read-modify-write lets the CLI and the dashboard editor silently drop each other's
    edit to a different loop. The re-read happens inside the transaction, which SQLite's
    ``IMMEDIATE`` mode makes a real compare-and-swap: the first writer holds the reserved
    lock for the whole block, so the second reads the map the first already wrote.
    """
    state = value.strip().lower()
    if state not in ENTRY_STATES:
        msg = f"invalid entry value {value!r}; use on|off"
        raise PresetEditError(msg)
    with transaction.atomic():
        preset = require_preset(preset_name)
        preset.entries = apply_entry_edits(preset.entries, [f"{loop_name}={state}"], preset_name=preset.name)
        preset.save(update_fields=["entries", "updated_at"])
    return preset


def activate_preset(name: str, *, reason: str, expected_lift_at: dt.datetime | None = None) -> None:
    """Activate *name* as the manual override through the mode-override chokepoint.

    It holds until someone clears it; *expected_lift_at* is advisory (A5/A7).
    """
    require_preset(name)
    set_mode_override(name, reason=reason, expected_lift_at=expected_lift_at)


def clear_preset_override() -> bool:
    """Clear the manual override so the active schedule decides again."""
    return clear_mode_override()


__all__ = [
    "ENTRY_OFF",
    "ENTRY_ON",
    "ENTRY_STATES",
    "PresetEditError",
    "activate_preset",
    "apply_entry_edits",
    "clear_preset_override",
    "entry_state_of",
    "refuse_unrelieved_backup",
    "require_preset",
    "set_preset_entry",
]
