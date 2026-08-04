"""The unified operating-mode resolver — one reader for the merged Mode (#61).

Availability and loop presets (#3159) were two parallel override→schedule→default
machines over different substrate. They are now ONE: a
:class:`~teatree.core.models.Mode` (the merged *Mode*) carries both the loop mask AND
the three intrinsic availability booleans, and this module resolves the single active
mode every Django consumer reads. The bare hooks read the SAME rows Django-free
through :func:`teatree.config.cold_mode.resolve_cold_posture` (#3826 deleted the
mirror file that used to stand between them and drift a week out of date).

The precedence chain (design §2.3) reuses the DB override/schedule resolver that
already backs presets — :func:`teatree.loop.preset_resolution.resolve_active_preset`
(L3 manual :class:`ModeOverride` row → L2 active-schedule slot) — and adds
the two pieces availability contributed:

*   **L0 default** — the configured ``default_mode`` ``ConfigSetting`` (default
    ``engaged``) when no override / schedule governs, replacing availability's
    ``present``-when-no-windows default.
*   **presence-sensitivity upgrade** — a fresh keystroke (within
    :data:`teatree.live_presence.PRESENCE_FRESHNESS`) upgrades an away-class
    mode reached *by schedule / default* to the ``presence_upgrade_mode`` (default
    ``engaged``). Upgrade-only; never downgrades; never overrides a manual override.

The returned :class:`ResolvedMode` satisfies every old surface at once: the
availability ``.defers_questions`` / ``.pauses_self_pump`` predicates AND the
preset ``.state_for`` per-loop opinion — so a consumer swaps its import and reads
one object.

Fail-open: any resolution error degrades to a safe present-class default mode with
a WARNING (mirroring both old resolvers), so a broken mode config can never brick
the loop fleet or silently mute the user.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.live_presence import PRESENCE, PRESENCE_FRESHNESS
from teatree.loop.preset_resolution import resolve_active_preset
from teatree.request_cache import cached_per_request

if TYPE_CHECKING:
    from teatree.core.models import Mode

logger = logging.getLogger(__name__)

# The L0 default mode when no override / schedule governs (design §2.3). Replaces
# availability's "present when no windows" default — a fresh box with no schedule
# resolves ``engaged`` (present-class), so it is never silently muted.
DEFAULT_MODE_SETTING = "default_mode"
#: Used only when the setting is unset AND no row of that name exists — the
#: synthesized present-class fallback. Every other consumer resolves by row.
FALLBACK_DEFAULT_MODE = "engaged"

# The present-class mode a live keystroke upgrades a schedule/default away-class
# mode to (design §3.3 / owner decision B). Re-pointable; defaults ``engaged``.
PRESENCE_UPGRADE_SETTING = "presence_upgrade_mode"
FALLBACK_UPGRADE_MODE = "engaged"

#: The dash switch's posture vocabulary: a UI action token as the intrinsic posture
#: ``(defers_questions, pauses_self_pump)`` it means. Each token resolves to the mode
#: carrying that posture BY ROW (:func:`mode_name_for_posture`), never by a hard-coded
#: mode name — so an operator renaming ``offline`` cannot break the switch. An INPUT
#: vocabulary only: nothing here is ever persisted or read back as state.
POSTURE_TOKENS: dict[str, tuple[bool, bool]] = {
    "reachable": (False, False),
    "defer-questions": (True, False),
    "pause-everything": (True, True),
}


@dataclass(frozen=True, slots=True)
class ResolvedMode:
    """The active mode plus the layer that decided it and when its tenure ends.

    Shaped so BOTH old surfaces are satisfied by one object: availability's
    ``.defers_questions`` / ``.pauses_self_pump`` predicates and the preset's
    ``.state_for`` per-loop opinion / ``.until`` boundary.
    """

    mode: "Mode"
    source: str  # "override" | "schedule" | "live" | "default"
    until: dt.datetime | None
    #: The human "why this mode governs" the observability surfaces render beside a
    #: per-loop verdict — the schedule slot / override wording, or the L0 / presence
    #: layer that supplied the mode when no preset did.
    reason: str = ""

    @property
    def name(self) -> str:
        return self.mode.name

    @property
    def defers_questions(self) -> bool:
        """``AskUserQuestion`` defers to the durable backlog (away + autonomous-away)."""
        return bool(self.mode.defers_questions)

    @property
    def pauses_self_pump(self) -> bool:
        """The Stop self-pump is suppressed — holiday-away only."""
        return bool(self.mode.pauses_self_pump)

    def state_for(self, loop_name: str) -> bool | None:
        """The tri-state per-loop opinion of the active mode's loop mask."""
        return self.mode.state_for(loop_name)


@cached_per_request
def resolve_active_mode(now: dt.datetime | None = None) -> ResolvedMode:
    """The single active operating mode at *now* (design §2.3).

    L3 manual override → L2 active-schedule slot → L0 configured default, then the
    presence-sensitivity upgrade. Fail-open to a synthesized present-class default
    on any error, so a consumer never crashes or silently mutes on a broken config.
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
    """Upgrade a schedule/default away-class mode to the present-class mode on a live keystroke.

    Only a mode reached by ``schedule`` / ``default`` that is ``presence_sensitive``
    AND ``defers_questions`` is a candidate — a manual override (source
    ``override``) is authoritative and never upgraded. A fresh keystroke within the
    presence-freshness window is direct evidence the user is at the keyboard now, so
    it beats the schedule's heuristic guess (the #58-era live-presence rule).
    """
    if resolved.source not in {"schedule", "default"}:
        return resolved
    if not resolved.mode.presence_sensitive or not resolved.mode.defers_questions:
        return resolved
    if not _fresh_keystroke(now):
        return resolved
    upgrade = _mode_by_name(_presence_upgrade_mode_name()) or _synthetic_default_mode()
    return ResolvedMode(mode=upgrade, source="live", until=None, reason=f"live keystroke upgraded {resolved.name}")


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


def mode_name_for_posture(token: str) -> str:
    """The name of the mode row carrying *token*'s posture, resolved by row not by literal.

    Raises :class:`LookupError` for an unknown token or when no seeded mode carries
    that posture — refusing to write an override naming a mode that does not exist,
    rather than leaving a dangling name that silently falls open to base config.
    """
    posture = POSTURE_TOKENS.get(token)
    if posture is None:
        msg = f"unknown posture token {token!r}; use {'/'.join(POSTURE_TOKENS)}"
        raise LookupError(msg)
    defers, pauses = posture
    mode = _mode_model().objects.by_posture(defers_questions=defers, pauses_self_pump=pauses)
    if mode is None:
        msg = f"no mode carries the {token!r} posture — run `t3 setup` to seed the defaults"
        raise LookupError(msg)
    return mode.name


def set_mode_override(
    name: str,
    *,
    until: dt.datetime | None = None,
    reason: str = "",
    user_id: str = "",
    overlay: str = "",
) -> None:
    """Set the manual mode override to *name*, draining the backlog on a return to reachable.

    The single L3 override write chokepoint every surface routes through — the
    ``t3 loop preset use`` CLI and the dash switch. It sets the DB ``ModeOverride``
    row, which is the ONE source of truth: the Django consumers resolve it through
    :func:`resolve_active_mode` and the bare hooks cold-read the same row through
    :func:`teatree.config.cold_mode.resolve_cold_posture` (#3826 deleted the mirror
    file that used to sit between them and drift). When the switch makes the resolved
    mode stop deferring (``defers_questions`` T→F, e.g. ``offline``→``engaged``), the
    deferred-question backlog auto-drains to the user's Slack DM. Fail-open: a drain
    failure never blocks the override write.
    """
    from teatree.core.models import ModeOverride  # noqa: PLC0415 — deferred: ORM needs the app registry

    before = resolve_active_mode().defers_questions
    ModeOverride.objects.set_override(name, until=until, reason=reason)
    _drain_if_returned(before_defers=before, user_id=user_id, overlay=overlay)


def clear_mode_override(*, user_id: str = "", overlay: str = "") -> bool:
    """Clear the manual mode override; drain the backlog if that returns to reachable."""
    from teatree.core.models import ModeOverride  # noqa: PLC0415 — deferred: ORM needs the app registry

    before = resolve_active_mode().defers_questions
    cleared = ModeOverride.objects.clear()
    _drain_if_returned(before_defers=before, user_id=user_id, overlay=overlay)
    return cleared


def posture_label(*, defers: bool, pauses: bool) -> str:
    """A human-readable name for the mode's posture — DISPLAY ONLY (#3826).

    Derived from the two booleans on every read; never stored, never an input, never
    an authority. The three legacy availability tokens it replaces
    (``present`` / ``autonomous_away`` / ``away``) were persisted and read back, which
    is how a stale serialization outlived the row it mirrored.
    """
    if not defers:
        return "reachable"
    return "unreachable (pump paused)" if pauses else "unreachable (factory running)"


def _drain_if_returned(*, before_defers: bool, user_id: str, overlay: str) -> None:
    """Fire the deferred-question drain when the resolved mode flips defers T→F (fail-open)."""
    if not before_defers or resolve_active_mode().defers_questions:
        return
    from teatree.core.notify_question_drains import drain_deferred_questions  # noqa: PLC0415 — deferred: cycle-safe

    try:
        drain_deferred_questions(user_id=user_id, overlay=overlay)
    except Exception as exc:  # noqa: BLE001 — drain is best-effort; never block the mode flip
        logger.warning("mode return→reachable auto-drain failed: %s", exc)


def _synthetic_default_mode() -> "Mode":
    """An UNSAVED present-class mode: no loop opinion (inherit base), never defers.

    The fail-open default when the configured default mode row is missing (a fresh
    DB before seeding, a deleted mode). Empty ``entries`` means every loop resolves
    ``state_for == None`` → inherit ``Loop.enabled``, i.e. byte-for-byte today's
    no-preset verdict; the booleans are present-class so nothing is muted.
    """
    return _mode_model()(
        name=FALLBACK_DEFAULT_MODE,
        entries={},
        defers_questions=False,
        pauses_self_pump=False,
        presence_sensitive=True,
    )


__all__ = [
    "DEFAULT_MODE_SETTING",
    "FALLBACK_DEFAULT_MODE",
    "FALLBACK_UPGRADE_MODE",
    "POSTURE_TOKENS",
    "PRESENCE_UPGRADE_SETTING",
    "ResolvedMode",
    "clear_mode_override",
    "mode_name_for_posture",
    "posture_label",
    "resolve_active_mode",
    "set_mode_override",
]
