"""Django-free cold read of the ACTIVE MODE's posture — the fast-hook seam (#3826).

The bare ``python3`` Stop/PreToolUse hooks must know whether the user is reachable
right now, and they cannot boot Django. Until #3826 they read a JSON mirror file under
the data dir that the Django override chokepoint wrote through — a SECOND source of
truth for the same concept, with no reconciliation. It drifted:
the mirror held a week-old ``autonomous_away`` while the override table was empty,
and every ``AskUserQuestion`` was silently deferred while the owner sat at the
keyboard.

The mirror only ever existed because a cold hook could not reach the DB. It can:
this module resolves the SAME precedence chain as
:func:`teatree.core.mode_resolution.resolve_active_mode` — L3 manual override → L2
active-schedule slot → L0 configured default, then the presence upgrade — straight
off the control DB in stdlib ``sqlite3``, over the same
:mod:`teatree.config.cold_db` seam the kill-switch flags already use. One source of
truth, two readers of it, no artifact in between.

**Fails toward ASKING, never toward silence.** The old design failed closed to the
most restrictive posture, which is how the owner was muted for a week. Every
unreadable / absent / malformed input here resolves to
:data:`UNRESOLVED` — reachable, questions ask, the pump runs — so a broken DB
surfaces as an interrupted human, not a silenced one.
"""

import datetime as dt
import json
import os
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from teatree.config.cold_db import canonical_config_db, fetch_all, fetch_one
from teatree.config.cold_reader import read_setting
from teatree.live_presence import PRESENCE_FILENAME, PRESENCE_FRESHNESS, parse_heartbeat
from teatree.paths import ControlDb

# Mirrors of the Django-side setting names + fallbacks (`teatree.core.mode_resolution`),
# pinned equal by `tests/quality/test_single_posture_reader.py` so the two readers of
# the one store cannot drift apart on WHICH rows they read.
ACTIVE_SCHEDULE_SETTING = "active_loop_schedule"
DEFAULT_MODE_SETTING = "default_mode"
PRESENCE_UPGRADE_SETTING = "presence_upgrade_mode"
FALLBACK_DEFAULT_MODE = "engaged"
FALLBACK_UPGRADE_MODE = "engaged"

# One week of candidate slot starts covers the schedule's week wrap (a Sunday-evening
# slot still governs Monday morning) — the same window `preset_resolution` searches.
_LOOKBACK_DAYS = 7
_MAX_WEEKDAY = 6


@dataclass(frozen=True, slots=True)
class ColdPosture:
    """The active mode's intrinsic posture and the layer that decided it.

    The cold twin of :class:`teatree.core.mode_resolution.ResolvedMode`'s two
    availability booleans. The mode NAME is deliberately absent: a hook decides
    whether to defer a question and whether to pump, never which mode row is active.
    """

    defers_questions: bool
    pauses_self_pump: bool
    source: str  # "override" | "schedule" | "live" | "default" | "unresolved"


def _reachable(source: str) -> ColdPosture:
    """A present-class posture decided by *source* — questions ask, the pump runs."""
    return ColdPosture(defers_questions=False, pauses_self_pump=False, source=source)


#: The fail-toward-asking verdict every unreadable input lands on — a cold read that
#: cannot decide must interrupt the user, never mute them.
UNRESOLVED = _reachable("unresolved")


@dataclass(frozen=True, slots=True)
class _ModeRow:
    """The three posture booleans of one ``teatree_loop_preset`` row."""

    defers_questions: bool
    pauses_self_pump: bool
    presence_sensitive: bool


def resolve_cold_posture(
    now: dt.datetime | None = None,
    *,
    db_path: Path | None = None,
    data_dir: Path | None = None,
    env: Mapping[str, str] = os.environ,
) -> ColdPosture:
    """The active mode's posture at *now*, read Django-free off the control DB.

    Same precedence as the Django resolver: L3 manual override → L2 active-schedule
    slot → L0 configured default, then the presence-sensitivity upgrade. Never
    raises: any failure resolves to :data:`UNRESOLVED` (ask the user).
    """
    moment = now or dt.datetime.now(tz=dt.UTC)
    try:
        return _resolve(moment, db_path=db_path, data_dir=data_dir, env=env)
    except Exception:  # noqa: BLE001 — cold hooks are crash-proof; an unreadable posture asks, never mutes
        return UNRESOLVED


def _resolve(now: dt.datetime, *, db_path: Path | None, data_dir: Path | None, env: Mapping[str, str]) -> ColdPosture:
    db = db_path if db_path is not None else canonical_config_db(env=env)
    if not db.exists():
        return UNRESOLVED
    mode, source = _governing_mode(db, now)
    if mode is None:
        return _reachable("default")
    if _upgrades_on_presence(mode, source) and _fresh_keystroke(now, data_dir=data_dir, env=env):
        upgrade = _mode_row(db, _setting_str(db, PRESENCE_UPGRADE_SETTING, FALLBACK_UPGRADE_MODE))
        return _posture(upgrade, "live") if upgrade is not None else _reachable("live")
    return _posture(mode, source)


def _governing_mode(db: Path, now: dt.datetime) -> tuple[_ModeRow | None, str]:
    """The mode row governing *now* and the layer that chose it (L3 → L2 → L0).

    A layer naming a DELETED mode falls straight through to the L0 default rather
    than to the layer below it — the same fail-open
    :func:`teatree.loop.preset_resolution._resolve_active_preset` performs, so a
    dangling override name never silently promotes the schedule.
    """
    active = _active_layer(db, now)
    if active is not None:
        mode = _mode_row(db, active[0])
        if mode is not None:
            return mode, active[1]
    return _mode_row(db, _setting_str(db, DEFAULT_MODE_SETTING, FALLBACK_DEFAULT_MODE)), "default"


def _active_layer(db: Path, now: dt.datetime) -> tuple[str, str] | None:
    """The mode NAME the L3 override or the L2 schedule slot picks, with its layer."""
    override = _override_name(db, now)
    if override is not None:
        return override, "override"
    scheduled = _scheduled_name(db, now)
    return (scheduled, "schedule") if scheduled is not None else None


def _posture(mode: _ModeRow, source: str) -> ColdPosture:
    return ColdPosture(defers_questions=mode.defers_questions, pauses_self_pump=mode.pauses_self_pump, source=source)


def _upgrades_on_presence(mode: _ModeRow, source: str) -> bool:
    """Whether a live keystroke may upgrade *mode* — the resolver's rule, mirrored.

    A manual override is authoritative and is never upgraded by a keystroke; only a
    presence-sensitive away-class mode reached by schedule / default is a candidate.
    """
    return source in {"schedule", "default"} and mode.presence_sensitive and mode.defers_questions


def _override_name(db: Path, now: dt.datetime) -> str | None:
    """The mode name the live L3 ``teatree_loop_preset_override`` row points at."""
    row = fetch_one(db, "SELECT preset_name, until FROM teatree_loop_preset_override ORDER BY set_at DESC LIMIT 1", ())
    if row is None:
        return None
    until = _parse_db_datetime(row[1])
    if until is not None and now >= until:
        return None
    return str(row[0])


def _scheduled_name(db: Path, now: dt.datetime) -> str | None:
    """The mode name the active schedule's governing slot picks at *now*."""
    name = _setting_str(db, ACTIVE_SCHEDULE_SETTING, "")
    if not name:
        return None
    schedule = fetch_one(db, "SELECT id, timezone FROM teatree_loop_schedule WHERE name=?", (name,))
    if schedule is None:
        return None
    slots = fetch_all(
        db, "SELECT days, start_time, preset_name FROM teatree_loop_schedule_slot WHERE schedule_id=?", (schedule[0],)
    )
    return _governing_slot(slots, now, _schedule_zone(str(schedule[1] or "")))


def _governing_slot(slots: list[tuple[object, ...]], now: dt.datetime, tz: dt.tzinfo) -> str | None:
    """The preset name of the latest slot start ≤ *now* over the ±7-day window.

    The coverage model of :func:`teatree.loop.preset_resolution._governing_and_next`:
    slot starts are local wall-clock times materialised as aware instants, so the
    governing slot is a single max over the window — no cron span arithmetic.
    """
    today = now.astimezone(tz).date()
    best: tuple[dt.datetime, str] | None = None
    for raw_days, raw_time, preset_name in slots:
        start_time = _parse_db_time(raw_time)
        if start_time is None:
            continue
        for offset in range(-_LOOKBACK_DAYS, _LOOKBACK_DAYS + 1):
            day = today + dt.timedelta(days=offset)
            if day.weekday() not in _weekdays(raw_days):
                continue
            start = dt.datetime.combine(day, start_time, tzinfo=tz)
            if start <= now and (best is None or start > best[0]):
                best = (start, str(preset_name))
    return None if best is None else best[1]


def _weekdays(raw: object) -> set[int]:
    """The slot's Mon=0..Sun=6 day ints from its JSON-stored ``days`` column."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str | bytes | bytearray) else raw
    except ValueError:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {day for day in parsed if isinstance(day, int) and 0 <= day <= _MAX_WEEKDAY}


def _mode_row(db: Path, name: str) -> _ModeRow | None:
    """The posture booleans of the ``teatree_loop_preset`` row named *name*."""
    if not name:
        return None
    row = fetch_one(
        db,
        "SELECT defers_questions, pauses_self_pump, presence_sensitive FROM teatree_loop_preset WHERE name=?",
        (name,),
    )
    if row is None:
        return None
    return _ModeRow(defers_questions=bool(row[0]), pauses_self_pump=bool(row[1]), presence_sensitive=bool(row[2]))


def _fresh_keystroke(now: dt.datetime, *, data_dir: Path | None, env: Mapping[str, str]) -> bool:
    """True when a ``UserPromptSubmit`` landed within the presence-freshness window."""
    root = data_dir if data_dir is not None else ControlDb(env).primary_data_dir()
    try:
        raw = (root / PRESENCE_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    at, _ = parse_heartbeat(raw)
    if at is None:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.UTC)
    return now - at <= PRESENCE_FRESHNESS


def _setting_str(db: Path, key: str, fallback: str) -> str:
    value = read_setting(key, db_path=db)
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _parse_db_datetime(value: object) -> dt.datetime | None:
    """Parse a Django sqlite ``DateTimeField`` column (UTC, naive) into an aware instant."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _parse_db_time(value: object) -> dt.time | None:
    """Parse a Django sqlite ``TimeField`` column (``HH:MM[:SS[.ffffff]]``)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.time.fromisoformat(value.strip())
    except ValueError:
        return None


def _schedule_zone(name: str) -> dt.tzinfo:
    """The schedule's IANA zone; UTC when unset or invalid.

    The Django resolver falls back to ``settings.TIME_ZONE``, which teatree pins to
    ``UTC`` — so the cold fallback is the same instant, without a Django settings read.
    """
    if name:
        try:
            return zoneinfo.ZoneInfo(name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            return dt.UTC
    return dt.UTC


__all__ = [
    "ACTIVE_SCHEDULE_SETTING",
    "DEFAULT_MODE_SETTING",
    "FALLBACK_DEFAULT_MODE",
    "FALLBACK_UPGRADE_MODE",
    "PRESENCE_UPGRADE_SETTING",
    "UNRESOLVED",
    "ColdPosture",
    "resolve_cold_posture",
]
