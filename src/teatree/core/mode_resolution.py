"""The unified operating-mode resolver — one reader for the merged Mode (#61).

A :class:`~teatree.core.models.Mode` is a pure per-loop on/off table, and this module
resolves the single active one every consumer reads.

The precedence chain reuses the DB override/schedule resolver that already backs presets
— :func:`teatree.loop.preset_resolution.resolve_active_preset` (manual
:class:`ModeOverride` row → active-schedule slot) — and adds the configured
``default_mode`` beneath it when neither governs. Three layers, all of them durable
writes or a calendar: what runs is decided top-down and nothing moves it sideways.

A keystroke used to upgrade a schedule/default mode to ``present``, and it was the one
arm that flipped with no observable event — raised by typing, lowered by the mere
absence of it — so a decision PERSISTED under one side of it could not be kept correct
by any chokepoint. Presence is still read for live-turn QUESTION routing
(:mod:`teatree.live_presence`); it no longer decides which loops run.

Fail-open: any resolution error degrades to a safe default mode with a WARNING, so a
broken mode config can never brick the loop fleet.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.utils import timezone

from teatree.core.session_identity import is_unattended_unauthorized_write
from teatree.loop.preset_resolution import resolve_preset_resolution
from teatree.request_cache import cached_per_request

if TYPE_CHECKING:
    from teatree.core.models import Mode

logger = logging.getLogger(__name__)

# The default mode when no override / schedule governs: a fresh box with no schedule
# resolves ``present``, so no loop is silently masked off.
DEFAULT_MODE_SETTING = "default_mode"
#: Used only when the setting is unset AND no row of that name exists — the
#: synthesized fallback. Every other consumer resolves by row.
FALLBACK_DEFAULT_MODE = "present"


@dataclass(frozen=True, slots=True)
class ResolvedMode:
    """The active mode plus the layer that decided it and when its tenure ends."""

    mode: "Mode"
    source: str  # "override" | "schedule" | "default"
    until: dt.datetime | None
    #: The human "why this mode governs" the observability surfaces render beside a
    #: per-loop verdict — the schedule slot / override wording, or the default layer
    #: that supplied the mode when no preset did.
    reason: str = ""
    #: Resolution FAILED, so this mode admits every loop. A real row answers for every
    #: loop and an unnamed one reads OFF; a resolution that could not answer at all must
    #: not inherit that, or a broken config would mask the whole fleet instead of failing
    #: open. The one reader is :meth:`state_for`.
    fail_open: bool = False

    @property
    def name(self) -> str:
        return self.mode.name

    def state_for(self, loop_name: str) -> bool:
        """Does the active mode run *loop_name*?"""
        return True if self.fail_open else self.mode.state_for(loop_name)


@cached_per_request
def resolve_active_mode(now: dt.datetime | None = None) -> ResolvedMode:
    """The single active operating mode at *now*.

    Manual override → active-schedule slot → configured default. Fail-open to a
    synthesized default on any error, so a consumer never crashes on a broken config.
    """
    moment = now or timezone.now()
    try:
        return _resolve_active_mode(moment)
    except Exception:
        logger.warning("mode resolution failed — failing open to admitting every loop", exc_info=True)
        return ResolvedMode(
            mode=_synthetic_default_mode(),
            source="default",
            until=None,
            reason="mode resolution failed",
            fail_open=True,
        )


def _resolve_active_mode(now: dt.datetime) -> ResolvedMode:
    resolution = resolve_preset_resolution(now)
    active = resolution.active
    if active is not None:
        return ResolvedMode(mode=active.preset, source=active.layer, until=active.until, reason=active.reason)
    configured = _mode_by_name(_default_mode_name())
    if configured is None:
        return ResolvedMode(
            mode=_synthetic_default_mode(),
            source="default",
            until=None,
            reason=f"{DEFAULT_MODE_SETTING} names no preset row",
            fail_open=True,
        )
    # A preset resolution that FAILED is not the absence of a preset: falling to the
    # permitting default on it would let an unreadable override speak as the owner.
    return ResolvedMode(
        mode=configured,
        source="default",
        until=None,
        reason="preset resolution failed" if resolution.failed else f"{DEFAULT_MODE_SETTING} setting",
        fail_open=resolution.failed,
    )


def _default_mode_name() -> str:
    return _setting_name(DEFAULT_MODE_SETTING, FALLBACK_DEFAULT_MODE)


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


def reject_unattended_posture_write(transition: str, *, authorized_by: str) -> None:
    """Refuse a posture change the unattended runner makes with nobody authorizing it.

    The posture alone decides the owner's voice, so writing it IS an authorization — the
    same class as a safety-posture config key, and it inherits that key's governance here
    rather than being free because it moved out of the settings table. Clearing is
    governed too: dropping an override exposes whatever schedule or default sits beneath.
    """
    if not is_unattended_unauthorized_write(authorized_by=authorized_by):
        return
    msg = (
        f"{transition} is not an unattended write: the posture decides whether the factory may act under "
        "the owner's identity, so its write IS an authorization. Set it from a session, or pass the "
        "identity that authorized it."
    )
    raise ValidationError(msg)


def set_mode_override(
    name: str, *, reason: str, expected_lift_at: dt.datetime | None = None, authorized_by: str = ""
) -> None:
    """Set the manual mode override to *name* — the single override write chokepoint.

    Every surface routes through here (the ``t3 loop preset use`` CLI and the dash
    switch). It sets the DB ``ModeOverride`` row, which is the ONE source of truth
    every consumer resolves through :func:`resolve_active_mode`.

    Raises :class:`LookupError` when no ``Mode`` row carries *name*, refusing to write
    an override that would silently fall open to base config, and
    :class:`~django.core.exceptions.ValidationError` when an unattended runner names no
    *authorized_by* (see :func:`reject_unattended_posture_write`).
    """
    from teatree.core.models import ModeOverride  # noqa: PLC0415 — deferred: ORM needs the app registry

    reject_unattended_posture_write(f"the loop preset override {name!r}", authorized_by=authorized_by)
    if _mode_by_name(name) is None:
        msg = f"unknown mode {name!r} — run `t3 loop preset list` for the defined modes"
        raise LookupError(msg)
    ModeOverride.objects.set_override(name, reason=reason, expected_lift_at=expected_lift_at)


def clear_mode_override(*, authorized_by: str = "") -> bool:
    """Clear the manual mode override so the schedule / default decides again."""
    from teatree.core.models import ModeOverride  # noqa: PLC0415 — deferred: ORM needs the app registry

    reject_unattended_posture_write("clearing the loop preset override", authorized_by=authorized_by)
    return ModeOverride.objects.clear()


def egress_forbidden(now: dt.datetime | None = None) -> bool:
    """Does the active posture forbid acting outward on the owner's behalf (B6)?

    The AFK line is per-ACTION, not per-loop: ``followup`` runs under ``afk`` while its
    colleague pings must not go out, so this is read at the on-behalf chokepoint rather
    than expressed by masking loops off. Read at SELECTION time, so it fails OPEN exactly as
    :func:`resolve_active_mode` does — a broken mode config must not stop the factory
    BUILDING its own work. Whether that work may then be SPOKEN is
    :func:`owner_voice_forbidden`, which answers the opposite way.
    """
    return resolve_active_mode(now).mode.forbids_egress


def owner_voice_forbidden(now: dt.datetime | None = None) -> bool:
    """May the factory act under the owner's identity right now? The chokepoint's question.

    Fail-CLOSED, and the asymmetry with :func:`egress_forbidden` is the point: a posture we
    cannot READ is not permission to speak as someone else, while the same unreadable posture
    must still let the factory build and queue its own work. Fail-open was sound only while a
    second per-post gate stood behind this one.
    """
    resolved = resolve_active_mode(now)
    return resolved.fail_open or resolved.mode.forbids_egress


def _synthetic_default_mode() -> "Mode":
    """An UNSAVED stand-in for a mode that could not be resolved (a fresh DB, a deleted row).

    It carries no entries and is only ever paired with ``fail_open=True``, which is what
    supplies the verdict — the row is there so surfaces have a name to render.
    """
    return _mode_model()(name=FALLBACK_DEFAULT_MODE, entries={})


__all__ = [
    "DEFAULT_MODE_SETTING",
    "FALLBACK_DEFAULT_MODE",
    "ResolvedMode",
    "clear_mode_override",
    "egress_forbidden",
    "owner_voice_forbidden",
    "reject_unattended_posture_write",
    "resolve_active_mode",
    "set_mode_override",
]
