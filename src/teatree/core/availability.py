"""Availability mode resolution — 24/7 dual question-mode (#58, §17.3 C3).

Two question modes (BLUEPRINT §17.1 invariant 9 / §17.3 C3 / §5.6.3):

* ``present`` — the user is reachable; ``AskUserQuestion`` runs interactively.
* ``away`` — the user is unreachable; the ``AskUserQuestion`` PreToolUse hook
    converts the tool call into a :class:`DeferredQuestion` row.

Mode resolution is a deterministic single-precedence chain (no fallback
mystery):

1. **Manual override** (unexpired) — recorded on disk by
    ``t3 availability away|present|auto`` and read here. ``auto`` clears
    the override so the schedule decides again.
2. **Cron-window schedule** — any active cron expression in
    ``[teatree.availability].windows`` evaluated in the configured
    timezone means ``present``; otherwise ``away``.
3. **Default** — ``present`` when no windows are configured (the
    conservative default: an agent without an availability config is
    present, never silently muted).

The override file is written via ``tmp.replace`` (atomic) so a torn
write never leaves a half-encoded JSON document; readers tolerating a
read race re-resolve cleanly.
"""

import json
import os
import tempfile
import tomllib
import zoneinfo
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from croniter import croniter

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.paths import DATA_DIR

MODE_PRESENT = "present"
MODE_AWAY = "away"
_VALID_MODES = frozenset({MODE_PRESENT, MODE_AWAY})

# Any active cron tick within this many seconds of *now* counts as
# "present". The schedule format is cron, which is a fire-time format
# rather than a span; the window is the inverse — "did the cron fire
# within the past <step> seconds?" — and a one-minute window matches
# the smallest cron resolution.
_WINDOW_SECONDS = 60


def override_path() -> Path:
    """Location of the durable availability-override JSON file."""
    return DATA_DIR / "availability_override.json"


@dataclass(frozen=True, slots=True)
class Override:
    """An unexpired manual override of the schedule."""

    mode: str
    until: datetime | None

    def is_active(self, now: datetime) -> bool:
        if self.mode not in _VALID_MODES:
            return False
        if self.until is None:
            return True
        return now < self.until


@dataclass(frozen=True, slots=True)
class Schedule:
    """Parsed ``[teatree.availability]`` cron-window config."""

    timezone: str = ""
    windows: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_toml(cls, raw: dict | None) -> "Schedule":
        """Parse a ``[teatree.availability]`` section into a schedule.

        Invalid cron expressions are dropped silently rather than raising
        — a broken schedule must never lock the agent out of asking
        questions. The default (empty schedule) means ``present``.
        """
        if not isinstance(raw, dict):
            return cls()
        tz = str(raw.get("timezone", "")).strip()
        raw_windows = raw.get("windows", [])
        if not isinstance(raw_windows, list):
            raw_windows = []
        validated: list[str] = []
        for entry in raw_windows:
            if not isinstance(entry, str):
                continue
            expr = entry.strip()
            if expr and croniter.is_valid(expr):
                validated.append(expr)
        return cls(timezone=tz, windows=tuple(validated))

    def is_present_at(self, when: datetime) -> bool:
        """True if any configured cron window is active at *when*.

        Empty schedule means ``present`` — the conservative default
        (an agent without a configured schedule answers in-band).
        """
        if not self.windows:
            return True
        local_when = self._localize(when)
        for expr in self.windows:
            base = local_when - timedelta(seconds=_WINDOW_SECONDS)
            itr = croniter(expr, base)
            fire = itr.get_next(datetime)
            if fire <= local_when:
                return True
        return False

    def _localize(self, when: datetime) -> datetime:
        if not self.timezone:
            return when
        try:
            tz = zoneinfo.ZoneInfo(self.timezone)
        except zoneinfo.ZoneInfoNotFoundError:
            return when
        if when.tzinfo is None:
            return when.replace(tzinfo=UTC).astimezone(tz)
        return when.astimezone(tz)


@dataclass(frozen=True, slots=True)
class Resolution:
    """The resolved availability mode plus the source that decided it."""

    mode: str
    source: str  # "override" | "schedule" | "default"


_MISSING_OVERRIDE: object = object()


def resolve_mode(
    *,
    now: datetime | None = None,
    schedule: Schedule | None = None,
    override: object = _MISSING_OVERRIDE,
) -> Resolution:
    """Resolve the effective mode at *now* by the §17.1 invariant 9 precedence.

    Override → schedule → default. Each layer is independently testable
    by passing it explicitly; the production path reads override from
    :func:`load_override` and schedule from :func:`load_schedule`.
    """
    moment = now or datetime.now(tz=UTC)
    eff_override = load_override() if override is _MISSING_OVERRIDE else override
    if isinstance(eff_override, Override) and eff_override.is_active(moment):
        return Resolution(mode=eff_override.mode, source="override")
    eff_schedule = schedule if schedule is not None else load_schedule()
    if eff_schedule.windows:
        mode = MODE_PRESENT if eff_schedule.is_present_at(moment) else MODE_AWAY
        return Resolution(mode=mode, source="schedule")
    return Resolution(mode=MODE_PRESENT, source="default")


def load_schedule(path: Path | None = None) -> Schedule:
    """Load the schedule from ``~/.teatree.toml``'s ``[teatree.availability]``."""
    config_path = path or Path(os.environ.get("TEATREE_TOML", str(Path.home() / ".teatree.toml")))
    if not config_path.is_file():
        return Schedule()
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return Schedule()
    section = data.get("teatree", {}).get("availability", {})
    if not isinstance(section, dict):
        return Schedule()
    return Schedule.from_toml(section)


def load_override(path: Path | None = None) -> Override | None:
    """Read the durable override file, if present and well-formed.

    A malformed or unreadable override returns ``None`` rather than
    raising — the resolver then falls through to the schedule, so the
    user is never blocked by a corrupt override file.
    """
    target = path or override_path()
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("mode", "")).strip().lower()
    if mode not in _VALID_MODES:
        return None
    until_raw = raw.get("until")
    until: datetime | None = None
    if isinstance(until_raw, str) and until_raw.strip():
        try:
            until = datetime.fromisoformat(until_raw)
        except ValueError:
            return None
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
    return Override(mode=mode, until=until)


def write_override(mode: str, *, until: datetime | None = None, path: Path | None = None) -> Path:
    """Write the override atomically via ``tmp.replace``.

    ``mode`` must be one of ``"present"`` / ``"away"``. ``until`` is an
    optional aware-datetime; ``None`` means the override never expires
    on its own (it is cleared explicitly with :func:`clear_override`).
    """
    if mode not in _VALID_MODES:
        msg = f"mode must be 'present' or 'away', got {mode!r}"
        raise ValueError(msg)
    target = path or override_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, str] = {"mode": mode}
    if until is not None:
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        payload["until"] = until.isoformat()
    fd, tmp_str = tempfile.mkstemp(prefix=".override-", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
        tmp_path.replace(target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def clear_override(path: Path | None = None) -> bool:
    """Delete the override file. Returns True if a file was removed."""
    target = path or override_path()
    if not target.exists():
        return False
    target.unlink()
    return True


def pending_questions_count(*, using: str | None = None) -> int:
    """Number of unresolved :class:`DeferredQuestion` rows (for statusline)."""
    return DeferredQuestion.pending(using=using).count()


def iter_pending_questions(*, using: str | None = None) -> Iterable[DeferredQuestion]:
    """Yield the unresolved :class:`DeferredQuestion` queue, oldest first."""
    return DeferredQuestion.pending(using=using)


__all__ = [
    "MODE_AWAY",
    "MODE_PRESENT",
    "Override",
    "Resolution",
    "Schedule",
    "clear_override",
    "iter_pending_questions",
    "load_override",
    "load_schedule",
    "override_path",
    "pending_questions_count",
    "resolve_mode",
    "write_override",
]
