"""The unified operating-mode resolver — one reader for the merged Mode (#61).

Availability (``present`` / ``autonomous_away`` / ``away``) and loop presets
(#3159) were two parallel override→schedule→default machines over different
substrate. They are now ONE: a :class:`~teatree.core.models.Mode` (the
merged *Mode*) carries both the loop mask AND the three intrinsic availability
booleans, and this module resolves the single active mode every consumer reads.

The precedence chain (design §2.3) reuses the DB override/schedule resolver that
already backs presets — :func:`teatree.loop.preset_resolution.resolve_active_preset`
(L3 manual :class:`ModeOverride` row → L2 active-schedule slot) — and adds
the two pieces availability contributed:

*   **L0 default** — the configured ``default_mode`` ``ConfigSetting`` (default
    ``engaged``) when no override / schedule governs, replacing availability's
    ``present``-when-no-windows default.
*   **presence-sensitivity upgrade** — a fresh keystroke (within
    :data:`teatree.core.availability.PRESENCE_FRESHNESS`) upgrades an away-class
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
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.loop.preset_resolution import resolve_active_preset

if TYPE_CHECKING:
    from teatree.core.models import Mode

logger = logging.getLogger(__name__)

# The L0 default mode when no override / schedule governs (design §2.3). Replaces
# availability's "present when no windows" default — a fresh box with no schedule
# resolves ``engaged`` (present-class), so it is never silently muted.
DEFAULT_MODE_SETTING = "default_mode"
_FALLBACK_DEFAULT_MODE = "engaged"

# The present-class mode a live keystroke upgrades a schedule/default away-class
# mode to (design §3.3 / owner decision B). Re-pointable; defaults ``engaged``.
PRESENCE_UPGRADE_SETTING = "presence_upgrade_mode"
_FALLBACK_UPGRADE_MODE = "engaged"


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
        return ResolvedMode(mode=_synthetic_default_mode(), source="default", until=None)


def _resolve_active_mode(now: dt.datetime) -> ResolvedMode:
    active = resolve_active_preset(now)
    if active is not None:
        resolved = ResolvedMode(mode=active.preset, source=active.layer, until=active.until)
    else:
        resolved = ResolvedMode(mode=_default_mode(), source="default", until=None)
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
    return ResolvedMode(mode=upgrade, source="live", until=None)


def _fresh_keystroke(now: dt.datetime) -> bool:
    """True when a ``UserPromptSubmit`` landed within the presence-freshness window."""
    # Deferred import keeps this domain module light and avoids an import cycle with
    # the availability shim (which delegates back into this resolver).
    from teatree.core.availability import PRESENCE, PRESENCE_FRESHNESS  # noqa: PLC0415 — deferred: cycle-safe

    last_seen = PRESENCE.last_seen()
    return last_seen is not None and now - last_seen <= PRESENCE_FRESHNESS


def _default_mode() -> "Mode":
    """The configured L0 default mode row, or a synthesized present-class fallback."""
    return _mode_by_name(_default_mode_name()) or _synthetic_default_mode()


def _default_mode_name() -> str:
    return _setting_name(DEFAULT_MODE_SETTING, _FALLBACK_DEFAULT_MODE)


def _presence_upgrade_mode_name() -> str:
    return _setting_name(PRESENCE_UPGRADE_SETTING, _FALLBACK_UPGRADE_MODE)


def _setting_name(key: str, fallback: str) -> str:
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — deferred: ORM needs the app registry

    raw = ConfigSetting.objects.get_effective(key)
    return raw.strip() if isinstance(raw, str) and raw.strip() else fallback


def _mode_by_name(name: str) -> "Mode | None":
    from teatree.core.models import Mode  # noqa: PLC0415 — deferred: ORM needs the app registry

    return Mode.objects.by_name(name)


def set_mode_override(
    name: str,
    *,
    until: dt.datetime | None = None,
    reason: str = "",
    user_id: str = "",
    overlay: str = "",
) -> None:
    """Set the manual mode override to *name*, draining the backlog on a return to reachable.

    The single L3 override write chokepoint the CLI (``t3 loop preset use`` and the
    deprecated availability aliases) and the dash switch route through — it sets the
    DB ``ModeOverride`` row (authoritative) and mirrors the posture to the fast-hook
    file. When the switch makes the resolved mode stop deferring
    (``defers_questions`` T→F, e.g. ``offline``→``engaged``), the deferred-question
    backlog auto-drains to the user's Slack DM, exactly as returning to ``present``
    did. Fail-open: a drain failure never blocks the override write.
    """
    from teatree.core.models import ModeOverride  # noqa: PLC0415 — deferred: ORM needs the app registry

    before = resolve_active_mode().defers_questions
    ModeOverride.objects.set_override(name, until=until, reason=reason)
    _mirror_posture_to_fast_hook_file(until=until)
    _drain_if_returned(before_defers=before, user_id=user_id, overlay=overlay)


def clear_mode_override(*, user_id: str = "", overlay: str = "") -> bool:
    """Clear the manual mode override; drain the backlog if that returns to reachable."""
    from teatree.core.models import ModeOverride  # noqa: PLC0415 — deferred: ORM needs the app registry

    before = resolve_active_mode().defers_questions
    cleared = ModeOverride.objects.clear()
    _mirror_posture_to_fast_hook_file(until=None)
    _drain_if_returned(before_defers=before, user_id=user_id, overlay=overlay)
    return cleared


def _mirror_posture_to_fast_hook_file(*, until: dt.datetime | None) -> None:
    """Mirror the newly-resolved availability posture into the fast-hook probe file.

    The stdlib away-probe (``hooks/scripts/availability_away_probe.py``) that gates
    AskUserQuestion deferral and the self-pump pause reads the legacy
    ``availability_override.json`` directly (no Django boot). The DB ``ModeOverride``
    row stays authoritative for every Django consumer; this write-through keeps the
    bare hooks in parity with the merged mode. ``drain=False`` — this module owns the
    single DB-authoritative drain. Fail-open: a file-write failure never blocks the
    override.
    """
    from teatree.core import availability  # noqa: PLC0415 — deferred: cycle-safe

    resolved = resolve_active_mode()
    token = _legacy_token(defers=resolved.defers_questions, pauses=resolved.pauses_self_pump)
    try:
        if token == availability.MODE_PRESENT and resolved.source in {"default", "live"}:
            # No manual/scheduled away posture to mirror — let the probe's own
            # default/schedule tiers decide, exactly as clearing the file does.
            availability.clear_override()
        else:
            _write_override_json(availability.override_path(), mode=token, until=until)
    except Exception as exc:  # noqa: BLE001 — fast-hook mirror is best-effort; never block the override
        logger.warning("fast-hook posture mirror failed: %s", exc)


def _write_override_json(target: Path, *, mode: str, until: dt.datetime | None) -> None:
    """Atomically write the availability override file — NO drain, NO resolve.

    The fast-hook posture mirror's pure writer (kept here, not in
    ``core.availability``, so that module's grandfathered LOC budget is untouched).
    The DB ``ModeOverride`` row is authoritative; this file only lets the stdlib
    away-probe read the resolved posture.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, str] = {"mode": mode}
    if until is not None:
        aware = until.replace(tzinfo=dt.UTC) if until.tzinfo is None else until
        payload["until"] = aware.isoformat()
    fd, tmp_str = tempfile.mkstemp(prefix=".override-", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _legacy_token(*, defers: bool, pauses: bool) -> str:
    """Map the merged mode's two booleans back to the legacy availability string.

    ``(F,*)`` → ``present`` (reachable), ``(T,F)`` → ``autonomous_away`` (defer but
    keep pumping), ``(T,T)`` → ``away`` (holiday). The single point the fast-hook
    file mirror translates the merged posture the stdlib probe understands.
    """
    from teatree.core import availability  # noqa: PLC0415 — deferred: cycle-safe

    if not defers:
        return availability.MODE_PRESENT
    return availability.MODE_AWAY if pauses else availability.MODE_AUTONOMOUS_AWAY


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
    from teatree.core.models import Mode  # noqa: PLC0415 — deferred: ORM needs the app registry

    return Mode(
        name=_FALLBACK_DEFAULT_MODE,
        entries={},
        defers_questions=False,
        pauses_self_pump=False,
        presence_sensitive=True,
    )


__all__ = [
    "DEFAULT_MODE_SETTING",
    "PRESENCE_UPGRADE_SETTING",
    "ResolvedMode",
    "clear_mode_override",
    "resolve_active_mode",
    "set_mode_override",
]
