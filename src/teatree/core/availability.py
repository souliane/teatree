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
import logging
import os
import tempfile
import tomllib
import warnings
import zoneinfo
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from croniter import croniter

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.notify import drain_deferred_questions
from teatree.paths import DATA_DIR

logger = logging.getLogger(__name__)

MODE_PRESENT = "present"
MODE_AWAY = "away"
_VALID_MODES = frozenset({MODE_PRESENT, MODE_AWAY})

# A query landing exactly on a fire instant counts as inside that fire's span.
_CRON_EPSILON = timedelta(microseconds=1)

# A single fire presents for its natural cadence (the smallest gap between
# consecutive fires), but never longer than this. Without the cap a sparse
# cron like ``0 9 * * 1-5`` — whose only inter-fire gap is ~1 day (or ~3 days
# across a weekend) — would present continuously for that whole gap.
_MAX_SPAN = timedelta(hours=1)

# How many consecutive fires to sample when measuring a cron's cadence. Six
# spans every realistic shape (hourly ranges, twice-daily, daily) — enough to
# observe the smallest gap even for irregular sets like ``0 9,17``.
_CADENCE_SAMPLE = 6


def override_path() -> Path:
    """Location of the durable availability-override JSON file."""
    return DATA_DIR / "availability_override.json"


def _validated_timezone(tz: str) -> str:
    """Return *tz* if it names a valid ``zoneinfo`` key, otherwise ``""``."""
    if not tz:
        return ""
    try:
        zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return ""
    else:
        return tz


def _cron_cadence(expr: str, anchor: datetime) -> timedelta:
    """Smallest gap between consecutive fires of *expr* near *anchor*.

    This is the cron's natural cadence — 1 minute for ``* …``, 1 hour for
    ``0 …`` over an hour range, ~1 day for a single daily fire. It is what a
    fire "covers" as a span, before the :data:`_MAX_SPAN` cap is applied.
    """
    itr = croniter(expr, anchor)
    fires = [itr.get_next(datetime) for _ in range(_CADENCE_SAMPLE)]
    return min(b - a for a, b in pairwise(fires))


def _is_sparse_window(expr: str) -> bool:
    """True when *expr* fires less often than :data:`_MAX_SPAN`.

    Such a window presents for only ~1 hour per fire, not as a continuous
    span — e.g. ``0 9 * * 1-5`` is present roughly 09:00-09:59, not all day.
    """
    return _cron_cadence(expr, datetime(2000, 1, 1, tzinfo=UTC)) > _MAX_SPAN


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
        tz_raw = str(raw.get("timezone", "")).strip()
        tz = _validated_timezone(tz_raw)
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
                if _is_sparse_window(expr):
                    warnings.warn(
                        f"availability window {expr!r} fires sparsely: it marks "
                        f"you present for only ~1 hour per fire, not as a "
                        f"continuous span. Use a per-minute or per-hour range "
                        f"(e.g. '* 9-16 * * 1-5') to be present across a window.",
                        stacklevel=2,
                    )
        return cls(timezone=tz, windows=tuple(validated))

    def is_present_at(self, when: datetime) -> bool:
        """True if any configured cron window is active at *when*.

        Empty schedule means ``present`` — the conservative default
        (an agent without a configured schedule answers in-band).

        A cron expression is a fire-time pattern, not a duration.  To
        evaluate it as a *span*, we find the most recent fire (``get_prev``)
        and the span it covers — its natural cadence (:func:`_cron_cadence`),
        capped at :data:`_MAX_SPAN`.  *when* is "present" when it lands within
        that span of the last fire.  Consecutive hourly fires therefore cover
        the full hour, per-minute fires cover continuously, and a sparse
        single-fire cron covers only its own hour (not the whole gap to its
        next fire, which would over-present for up to ~3 days across a
        weekend).
        """
        if not self.windows:
            return True
        local_when = self._localize(when)
        start = local_when + _CRON_EPSILON
        for expr in self.windows:
            prev = croniter(expr, start).get_prev(datetime)
            span = min(_cron_cadence(expr, prev - _CRON_EPSILON), _MAX_SPAN)
            if start - prev <= span:
                return True
        return False

    def _localize(self, when: datetime) -> datetime:
        if not self.timezone:
            return when
        try:
            tz = zoneinfo.ZoneInfo(self.timezone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
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


def write_override(
    mode: str,
    *,
    until: datetime | None = None,
    path: Path | None = None,
    user_id: str = "",
    overlay: str = "",
) -> Path:
    """Write the override atomically via ``tmp.replace``.

    ``mode`` must be one of ``"present"`` / ``"away"``. ``until`` is an
    optional aware-datetime; ``None`` means the override never expires
    on its own (it is cleared explicitly with :func:`clear_override`).

    Setting ``present`` from a prior effective mode of ``away`` is the
    canonical away→present transition: it auto-drains the deferred-question
    backlog to the user's Slack DM (the user reads Slack, not the CLI), so
    returning never silently swallows questions and never depends on the
    agent remembering to run ``t3 questions resurface``. The drain only
    fires on an actual transition — setting present while already present
    is a no-op — and is fully fail-open: a Slack failure is swallowed and
    never blocks the availability flip. ``user_id`` / ``overlay`` are
    forwarded to the drain for DM targeting and per-overlay bot routing.
    """
    if mode not in _VALID_MODES:
        msg = f"mode must be 'present' or 'away', got {mode!r}"
        raise ValueError(msg)
    target = path or override_path()
    prior_mode = resolve_mode().mode if mode == MODE_PRESENT else None
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
    if prior_mode == MODE_AWAY:
        _drain_on_return(user_id=user_id, overlay=overlay)
    return target


def _drain_on_return(*, user_id: str, overlay: str) -> None:
    """Auto-fire the away→present deferred-question drain, fail-open.

    Reuses the canonical :func:`teatree.core.notify.drain_deferred_questions`
    egress (the same code path ``t3 questions resurface`` runs). Any
    failure — a Slack outage, a missing backend, an import error — is
    swallowed so the availability flip that already landed on disk is never
    rolled back or made to raise.
    """
    try:
        drain_deferred_questions(user_id=user_id, overlay=overlay)
    except Exception as exc:  # noqa: BLE001 — drain is best-effort; never block the availability flip
        logger.warning("away→present auto-drain failed: %s", exc)


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
