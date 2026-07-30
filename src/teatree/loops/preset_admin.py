"""Preset lifecycle — create, rename, delete, and the operator-facing metadata (#3559).

A preset is referenced BY NAME from three places: the manual :class:`ModeOverride`
row, every :class:`ModeScheduleSlot`, and the settings whose value selects a preset.
So a rename here moves all of them in one transaction, and a delete is refused while
any of them still names the preset — a dangling name resolves to base config with a
warning, which is a fine failure mode to survive but a poor one to walk into.
"""

import re
from dataclasses import dataclass
from typing import Final

from django.db import transaction

from teatree.core.mode_resolution import (
    DEFAULT_MODE_SETTING,
    FALLBACK_DEFAULT_MODE,
    FALLBACK_UPGRADE_MODE,
    PRESENCE_UPGRADE_SETTING,
)
from teatree.core.models import ConfigSetting, Mode, ModeOverride, ModeScheduleSlot
from teatree.core.models.loop_preset import DEFAULT_LOW_POWER_PRESET, LOW_POWER_PRESET_SETTING, PIN_MODES
from teatree.loops.preset_editing import PresetEditError, require_preset

_SLUG_RE: Final = re.compile(r"^[-a-zA-Z0-9_]+$")

#: Every ``ConfigSetting`` key whose VALUE is a preset name, with the name that key
#: falls back to when unset. A rename re-points each of these — including one still
#: sitting on its unset default, so the default cannot dangle either.
_PRESET_NAME_SETTINGS: Final[tuple[tuple[str, str], ...]] = (
    (DEFAULT_MODE_SETTING, FALLBACK_DEFAULT_MODE),
    (PRESENCE_UPGRADE_SETTING, FALLBACK_UPGRADE_MODE),
    (LOW_POWER_PRESET_SETTING, DEFAULT_LOW_POWER_PRESET),
)


@dataclass(frozen=True, slots=True)
class PresetReferrers:
    """Everything resolving a preset by name — what a rename moves and a delete respects."""

    is_active: bool = False
    schedule_slots: tuple[str, ...] = ()
    settings: tuple[str, ...] = ()

    @property
    def blocks_delete(self) -> bool:
        return self.is_active or bool(self.schedule_slots) or bool(self.settings)

    @property
    def summary(self) -> str:
        """The human list of referrers, for the refusal message and the editor."""
        parts = []
        if self.is_active:
            parts.append("it is the active preset")
        if self.schedule_slots:
            parts.append("schedule slots " + ", ".join(self.schedule_slots))
        if self.settings:
            parts.append("settings " + ", ".join(self.settings))
        return "; ".join(parts)


def preset_referrers(name: str) -> PresetReferrers:
    """Every live by-name reference to *name* — the manual override, slots, and settings."""
    override = ModeOverride.objects.current()
    slots = ModeScheduleSlot.objects.filter(preset_name=name).select_related("schedule")
    return PresetReferrers(
        is_active=override is not None and override.preset_name == name,
        schedule_slots=tuple(f"{slot.schedule.name}@{slot.start_time:%H:%M}" for slot in slots),
        settings=tuple(key for key, _ in _PRESET_NAME_SETTINGS if _setting_selects(key) == name),
    )


def create_preset(name: str, *, description: str = "") -> Mode:
    """Create an empty preset — no opinion on any loop until entries are set."""
    slug = _validated_slug(name)
    if Mode.objects.by_name(slug) is not None:
        msg = f"preset {slug!r} already exists"
        raise PresetEditError(msg)
    return Mode.objects.create(name=slug, entries={}, description=description)


def update_preset_meta(name: str, *, description: str | None = None, availability_pin: str | None = None) -> Mode:
    """Edit a preset's operator-facing description and its availability pin.

    ``availability_pin=""`` CLEARS the pin — clearing must be expressible, not only
    switching between pins, so the argument is tri-state (``None`` leaves it alone).
    """
    preset = require_preset(name)
    if description is not None:
        preset.description = description
    if availability_pin is not None:
        preset.availability_mode = _validated_pin(availability_pin)
    preset.save(update_fields=["description", "availability_mode", "updated_at"])
    return preset


def rename_preset(name: str, new_name: str) -> Mode:
    """Rename a preset and re-point every by-name referrer in ONE transaction."""
    preset = require_preset(name)
    slug = _validated_slug(new_name)
    if slug != name and Mode.objects.by_name(slug) is not None:
        msg = f"preset {slug!r} already exists"
        raise PresetEditError(msg)
    with transaction.atomic():
        preset.name = slug
        preset.save(update_fields=["name", "updated_at"])
        ModeOverride.objects.filter(preset_name=name).update(preset_name=slug)
        ModeScheduleSlot.objects.filter(preset_name=name).update(preset_name=slug)
        for key, _ in _PRESET_NAME_SETTINGS:
            if _setting_selects(key) == name:
                ConfigSetting.objects.set_value(key, slug)
    return preset


def delete_preset(name: str) -> None:
    """Delete a preset, refusing while anything still resolves it by name."""
    require_preset(name)
    referrers = preset_referrers(name)
    if referrers.blocks_delete:
        msg = f"cannot delete preset {name!r} — {referrers.summary}"
        raise PresetEditError(msg)
    Mode.objects.filter(name=name).delete()


def _setting_selects(key: str) -> str:
    """The preset name a setting currently selects, falling back to its documented default."""
    raw = ConfigSetting.objects.get_effective(key)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return next(fallback for setting, fallback in _PRESET_NAME_SETTINGS if setting == key)


def _validated_slug(name: str) -> str:
    slug = name.strip()
    if not slug or not _SLUG_RE.match(slug):
        msg = f"invalid preset name {name!r}; use letters, digits, dashes and underscores"
        raise PresetEditError(msg)
    return slug


def _validated_pin(pin: str) -> str:
    value = pin.strip()
    if value and value not in PIN_MODES:
        msg = f"invalid availability pin {pin!r}; use {'|'.join(sorted(PIN_MODES))} or empty to clear"
        raise PresetEditError(msg)
    return value


__all__ = [
    "PresetReferrers",
    "create_preset",
    "delete_preset",
    "preset_referrers",
    "rename_preset",
    "update_preset_meta",
]
