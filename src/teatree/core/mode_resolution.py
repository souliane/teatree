"""The unified operating-mode resolver — one reader for the merged Mode (#61).

A :class:`~teatree.core.models.Mode` is a pure per-loop on/off table, and this module
resolves the single active one every consumer reads.

The precedence chain (design §2.3) reuses the DB override/schedule resolver that
already backs presets — :func:`teatree.loop.preset_resolution.resolve_active_preset`
(L3 manual :class:`ModeOverride` row → L2 active-schedule slot) — and adds two layers:

*   **L0 default** — the configured ``default_mode`` ``ConfigSetting`` (default
    ``present``) when no override / schedule governs.
*   **presence upgrade** — a fresh keystroke (within
    :data:`teatree.live_presence.PRESENCE_FRESHNESS`) upgrades a mode reached *by
    schedule / default* to the ``presence_upgrade_mode`` (default ``present``).
    Upgrade-only; never downgrades; never overrides a manual override.

Fail-open: any resolution error degrades to a safe default mode with a WARNING, so a
broken mode config can never brick the loop fleet.
"""

import datetime as dt
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.live_presence import PRESENCE, PRESENCE_FRESHNESS
from teatree.loop.preset_resolution import resolve_active_preset
from teatree.request_cache import cached_per_request

if TYPE_CHECKING:
    from teatree.core.models import Mode

logger = logging.getLogger(__name__)

# The L0 default mode when no override / schedule governs (design §2.3): a fresh box
# with no schedule resolves ``present``, so no loop is silently masked off.
DEFAULT_MODE_SETTING = "default_mode"
#: Used only when the setting is unset AND no row of that name exists — the
#: synthesized fallback. Every other consumer resolves by row.
FALLBACK_DEFAULT_MODE = "present"

# The mode a live keystroke upgrades a schedule/default mode to (design §3.3 / owner
# decision B). Re-pointable; defaults ``present``.
PRESENCE_UPGRADE_SETTING = "presence_upgrade_mode"
FALLBACK_UPGRADE_MODE = "present"

#: A keystroke beats the schedule's GUESS, never the operator's own deliberate override —
#: which is how `off` / `low-token` are pinned against a stray keystroke.
_UPGRADABLE_SOURCES = frozenset({"schedule", "default"})


@dataclass(frozen=True, slots=True)
class ResolvedMode:
    """The active mode plus the layer that decided it and when its tenure ends."""

    mode: "Mode"
    source: str  # "override" | "schedule" | "live" | "default"
    until: dt.datetime | None
    #: The human "why this mode governs" the observability surfaces render beside a
    #: per-loop verdict — the schedule slot / override wording, or the L0 / presence
    #: layer that supplied the mode when no preset did.
    reason: str = ""
    #: The OTHER side of the live-presence flip, when this mode sits on one side of it:
    #: the upgrade counterpart of an upgradable schedule/default mode, or the
    #: schedule/default mode an applied upgrade replaced. ``None`` when no keystroke can
    #: move this mode at all (a manual override, or a mode that is not upgradable).
    #:
    #: It exists because the presence upgrade is the ONE arm of the enable verdict that
    #: flips with no observable event — a keystroke raises it and the mere ABSENCE of one
    #: lowers it — so a decision PERSISTED under one side of it (a ``loop_timer`` chain)
    #: cannot be kept correct by any chokepoint. Chain membership closes over both sides
    #: instead; see :meth:`teatree.loops.enable_verdict.EnablePlanes.admits_any_mask`.
    presence_alternate: "Mode | None" = None

    @property
    def name(self) -> str:
        return self.mode.name

    def state_for(self, loop_name: str) -> bool | None:
        """The tri-state per-loop opinion of the active mode's loop mask."""
        return self.mode.state_for(loop_name)


@cached_per_request
def resolve_active_mode(now: dt.datetime | None = None) -> ResolvedMode:
    """The single active operating mode at *now* (design §2.3).

    L3 manual override → L2 active-schedule slot → L0 configured default, then the
    presence upgrade. Fail-open to a synthesized default on any error, so a consumer
    never crashes on a broken config.
    """
    moment = now or timezone.now()
    try:
        return _resolve_active_mode(moment)
    except Exception:
        logger.warning("mode resolution failed — failing open to a present-class default", exc_info=True)
        return ResolvedMode(
            mode=_synthetic_default_mode(), source="default", until=None, reason="mode resolution failed"
        )


def _resolve_active_mode(now: dt.datetime) -> ResolvedMode:
    active = resolve_active_preset(now)
    if active is not None:
        resolved = ResolvedMode(mode=active.preset, source=active.layer, until=active.until, reason=active.reason)
    else:
        resolved = ResolvedMode(
            mode=_default_mode(), source="default", until=None, reason=f"{DEFAULT_MODE_SETTING} setting"
        )
    return _apply_presence_upgrade(resolved, now)


def _apply_presence_upgrade(resolved: ResolvedMode, now: dt.datetime) -> ResolvedMode:
    """Upgrade a schedule/default mode to the ``presence_upgrade_mode`` on a live keystroke.

    Only a mode reached by ``schedule`` / ``default`` that is not ALREADY the upgrade
    target is a candidate — a manual override (source ``override``) is authoritative
    and never upgraded, which is what keeps the token-budget escape from being undone
    by a keystroke. A fresh keystroke within the presence-freshness window is direct
    evidence the user is at the keyboard now, so it beats the schedule's heuristic
    guess (the #58-era live-presence rule).

    Either way the mode carries its :attr:`~ResolvedMode.presence_alternate` — the side of
    the flip it is NOT on — so a reader that must survive the flip can close over both.
    This is the ONLY producer of that field.
    """
    if resolved.source not in _UPGRADABLE_SOURCES:
        return resolved
    # Read the target's NAME once and reuse it for both the precondition and the lookup:
    # this runs on every resolve, so a second settings read would be a per-tick query.
    upgrade_name = _presence_upgrade_mode_name()
    if resolved.name == upgrade_name:
        return resolved
    upgrade = _mode_by_name(upgrade_name) or _synthetic_default_mode()
    if not _fresh_keystroke(now):
        return replace(resolved, presence_alternate=upgrade)
    return ResolvedMode(
        mode=upgrade,
        source="live",
        until=None,
        reason=f"live keystroke upgraded {resolved.name}",
        presence_alternate=resolved.mode,
    )


def _fresh_keystroke(now: dt.datetime) -> bool:
    """True when a ``UserPromptSubmit`` landed within the presence-freshness window."""
    last_seen = PRESENCE.last_seen()
    return last_seen is not None and now - last_seen <= PRESENCE_FRESHNESS


def _default_mode() -> "Mode":
    """The configured L0 default mode row, or a synthesized present-class fallback."""
    return _mode_by_name(_default_mode_name()) or _synthetic_default_mode()


def _default_mode_name() -> str:
    return _setting_name(DEFAULT_MODE_SETTING, FALLBACK_DEFAULT_MODE)


def _presence_upgrade_mode_name() -> str:
    return _setting_name(PRESENCE_UPGRADE_SETTING, FALLBACK_UPGRADE_MODE)


def _setting_name(key: str, fallback: str) -> str:
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — deferred: ORM needs the app registry

    raw = ConfigSetting.objects.get_effective(key)
    return raw.strip() if isinstance(raw, str) and raw.strip() else fallback


def _mode_model() -> "type[Mode]":
    """The ``Mode`` model class — the ONE deferred ORM import every mode lookup here shares."""
    from teatree.core.models import Mode  # noqa: PLC0415 — deferred: ORM needs the app registry

    return Mode


def _mode_by_name(name: str) -> "Mode | None":
    return _mode_model().objects.by_name(name)


def set_mode_override(
    name: str,
    *,
    until: dt.datetime | None = None,
    reason: str = "",
) -> None:
    """Set the manual mode override to *name* — the single L3 write chokepoint.

    Every surface routes through here (the ``t3 loop preset use`` CLI and the dash
    switch). It sets the DB ``ModeOverride`` row, which is the ONE source of truth
    every consumer resolves through :func:`resolve_active_mode`.

    Raises :class:`LookupError` when no ``Mode`` row carries *name*, refusing to write
    an override that would silently fall open to base config.
    """
    from teatree.core.models import ModeOverride  # noqa: PLC0415 — deferred: ORM needs the app registry

    if _mode_by_name(name) is None:
        msg = f"unknown mode {name!r} — run `t3 loop preset list` for the defined modes"
        raise LookupError(msg)
    ModeOverride.objects.set_override(name, until=until, reason=reason)


def clear_mode_override() -> bool:
    """Clear the manual mode override so the schedule / default decides again."""
    from teatree.core.models import ModeOverride  # noqa: PLC0415 — deferred: ORM needs the app registry

    return ModeOverride.objects.clear()


def _synthetic_default_mode() -> "Mode":
    """An UNSAVED mode with no loop opinion — the fail-open default.

    Used when the configured default mode row is missing (a fresh DB before seeding,
    a deleted mode). Empty ``entries`` means every loop resolves ``state_for == None``
    → inherit ``Loop.enabled``, i.e. byte-for-byte the no-preset verdict.
    """
    return _mode_model()(name=FALLBACK_DEFAULT_MODE, entries={})


__all__ = [
    "DEFAULT_MODE_SETTING",
    "FALLBACK_DEFAULT_MODE",
    "FALLBACK_UPGRADE_MODE",
    "PRESENCE_UPGRADE_SETTING",
    "ResolvedMode",
    "clear_mode_override",
    "resolve_active_mode",
    "set_mode_override",
]
