"""Availability mode resolution — 24/7 dual question-mode (#58, §17.3 C3).

Three availability modes (BLUEPRINT §17.1 invariant 9 / §17.3 C3 / §5.6.3):

* ``present`` — the user is reachable; ``AskUserQuestion`` runs interactively
    and the self-pump keeps the loop moving.
* ``away`` — the user is on holiday; the ``AskUserQuestion`` PreToolUse hook
    converts the tool call into a :class:`DeferredQuestion` row AND the
    self-pump pauses (an explicit "stop self-driving too").
* ``autonomous_away`` (#2544) — the user is unreachable but the factory must
    keep running unattended: questions defer exactly like ``away`` yet the
    self-pump keeps firing exactly like ``present``. The two behaviours
    ``away`` conflated are split by :func:`mode_defers_questions` (away +
    autonomous_away) and :func:`mode_pauses_self_pump` (away only).

Mode resolution is a deterministic single-precedence chain (no fallback
mystery):

1. **Manual override** (unexpired) — recorded on disk by
    ``t3 availability away|present|auto`` and read here. ``auto`` clears
    the override so the schedule decides again. A deliberate ``away``
    override (a holiday) is authoritative — it wins over everything below.
2. **Live presence beats a schedule-derived ``away``** — a
    ``UserPromptSubmit`` recorded within :data:`PRESENCE_FRESHNESS` is
    direct evidence the user is at the keyboard *now*. The cron schedule
    is only a heuristic guess about reachability; a fresh prompt is
    ground truth, so it overrides a schedule that would otherwise mute a
    demonstrably-present user (the #58-era bug: a user actively typing
    outside their configured work hours had their ``AskUserQuestion``
    calls silently deferred). It only *upgrades* a schedule ``away`` to
    ``present`` — it never downgrades, and it never overrides an explicit
    manual override.
3. **Cron-window schedule** — any active cron expression in the DB-home
    ``availability_schedule`` setting's ``windows`` evaluated in the
    configured timezone means ``present``; otherwise ``away``.
4. **Default** — ``present`` when no windows are configured (the
    conservative default: an agent without an availability config is
    present, never silently muted).

The override and presence files are written via ``tmp.replace`` (atomic)
so a torn write never leaves a half-encoded document; readers tolerating
a read race re-resolve cleanly.
"""

import json
import logging
import os
import tempfile
import warnings
import zoneinfo
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import cast

from croniter import croniter

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.notify_question_drains import drain_deferred_questions
from teatree.paths import DATA_DIR

logger = logging.getLogger(__name__)

MODE_PRESENT = "present"
MODE_AWAY = "away"
# Autonomous-away (#2544): the user is unreachable (questions defer, exactly
# like holiday-away) but the factory keeps self-driving (the self-pump is NOT
# paused, exactly like present). It exists because a permanent holiday-``away``
# silently killed long unattended runs — ``away`` conflated "defer my questions"
# with "stop self-pumping", so an unattended operator got both-or-neither.
MODE_AUTONOMOUS_AWAY = "autonomous_away"
VALID_MODES = frozenset({MODE_PRESENT, MODE_AWAY, MODE_AUTONOMOUS_AWAY})

# Modes in which the user is unreachable NOW: questions defer to the durable
# backlog, local TTS is silenced, and returning to present drains the backlog.
_DEFERRING_MODES = frozenset({MODE_AWAY, MODE_AUTONOMOUS_AWAY})
# Modes that pause teatree's own self-pump (the standing keep-going directive).
# Only holiday-``away`` pauses; autonomous-away keeps the factory running.
_PAUSING_MODES = frozenset({MODE_AWAY})


# How recently a ``UserPromptSubmit`` must have landed for the user to count
# as demonstrably present. A live prompt within this window upgrades a
# schedule-derived ``away`` to ``present`` — long enough to bridge a normal
# pause between prompts, short enough that a user who walked away an hour ago
# is correctly treated as away by the schedule.
PRESENCE_FRESHNESS = timedelta(minutes=15)

# How recently a ``UserPromptSubmit`` must have landed, IN THIS SESSION, for the
# current turn to count as user-driven (#189). Intentionally far shorter than
# :data:`PRESENCE_FRESHNESS`: this is "the user typed THIS turn", not "the user
# is reachable today". It must outlive the few tool calls between the prompt and
# the agent's ``AskUserQuestion`` reply, but expire long before a follow-on
# autonomous turn could be mistaken for a fresh keystroke.
LIVE_TURN_FRESHNESS = timedelta(seconds=90)

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


def presence_path() -> Path:
    """Location of the durable live-presence heartbeat file."""
    return DATA_DIR / "availability_presence"


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
        if self.mode not in VALID_MODES:
            return False
        if self.until is None:
            return True
        return now < self.until

    @property
    def defers_questions(self) -> bool:
        """``AskUserQuestion`` defers to the durable backlog — away + autonomous-away (#2544)."""
        return self.mode in _DEFERRING_MODES

    @property
    def pauses_self_pump(self) -> bool:
        """The Stop self-pump is suppressed — holiday-``away`` only (#2544)."""
        return self.mode in _PAUSING_MODES


@dataclass(frozen=True, slots=True)
class Schedule:
    """Parsed availability cron-window config (the DB ``availability_schedule`` setting)."""

    timezone: str = ""
    windows: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_table(cls, raw: object) -> "Schedule":
        """Parse an ``availability_schedule`` table into a schedule.

        Invalid cron expressions are dropped silently rather than raising
        — a broken schedule must never lock the agent out of asking
        questions. The default (empty schedule) means ``present``.
        """
        if not isinstance(raw, dict):
            return cls()
        table = cast("dict[str, object]", raw)
        tz_raw = str(table.get("timezone", "")).strip()
        tz = _validated_timezone(tz_raw)
        raw_windows = table.get("windows", [])
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
    source: str  # "override" | "live" | "schedule" | "default"

    @property
    def defers_questions(self) -> bool:
        """``AskUserQuestion`` defers to the durable backlog — away + autonomous-away (#2544)."""
        return self.mode in _DEFERRING_MODES

    @property
    def pauses_self_pump(self) -> bool:
        """The Stop self-pump is suppressed — holiday-``away`` only (#2544)."""
        return self.mode in _PAUSING_MODES


_MISSING_OVERRIDE: object = object()


def resolve_mode(
    *,
    now: datetime | None = None,
    schedule: Schedule | None = None,
    override: object = _MISSING_OVERRIDE,
    presence: datetime | object | None = _MISSING_OVERRIDE,
) -> Resolution:
    """Resolve the effective mode at *now* by the §17.1 invariant 9 precedence.

    Override → live presence (upgrade-only) → schedule → default. Each layer
    is independently testable by passing it explicitly; the production path
    reads override from :func:`load_override`, schedule from
    :func:`load_schedule`, and the live-presence heartbeat from the
    :data:`PRESENCE` singleton's :meth:`PresenceHeartbeat.last_seen`.

    Live presence only ever upgrades a schedule-derived ``away`` to
    ``present`` — direct evidence (a recent ``UserPromptSubmit``) beats the
    schedule's heuristic guess about reachability. It never downgrades a
    present schedule and never overrides an explicit manual override.
    """
    moment = now or datetime.now(tz=UTC)
    eff_override = load_override() if override is _MISSING_OVERRIDE else override
    if isinstance(eff_override, Override) and eff_override.is_active(moment):
        return Resolution(mode=eff_override.mode, source="override")
    eff_schedule = schedule if schedule is not None else load_schedule()
    if eff_schedule.windows:
        if eff_schedule.is_present_at(moment):
            return Resolution(mode=MODE_PRESENT, source="schedule")
        eff_presence = PRESENCE.last_seen() if presence is _MISSING_OVERRIDE else presence
        if isinstance(eff_presence, datetime) and moment - eff_presence <= PRESENCE_FRESHNESS:
            return Resolution(mode=MODE_PRESENT, source="live")
        return Resolution(mode=MODE_AWAY, source="schedule")
    return Resolution(mode=MODE_PRESENT, source="default")


def load_schedule(db_path: Path | None = None) -> Schedule:
    """Load the schedule from the DB-home ``availability_schedule`` setting.

    Absence (no row, no DB) resolves to an empty :class:`Schedule` — the
    conservative ``present`` default.
    """
    # Deferred (PLC0415): importing `teatree.config` at module scope eagerly
    # loads its heavy package __init__; keep this module's import light.
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: call-time import, kept lazy

    return Schedule.from_table(cold_reader.read_setting("availability_schedule", db_path=db_path))


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
    if mode not in VALID_MODES:
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

    ``mode`` must be one of :data:`VALID_MODES` — ``"present"`` / ``"away"`` /
    ``"autonomous_away"``. ``until`` is an optional aware-datetime; ``None``
    means the override never expires on its own (it is cleared explicitly with
    :func:`clear_override`).

    Setting ``present`` from a prior deferring mode (``away`` /
    ``autonomous_away``) is the canonical away→present transition: it
    auto-drains the deferred-question
    backlog to the user's Slack DM (the user reads Slack, not the CLI), so
    returning never silently swallows questions and never depends on the
    agent remembering to run ``t3 teatree questions resurface``. The drain only
    fires on an actual transition — setting present while already present
    is a no-op — and is fully fail-open: a Slack failure is swallowed and
    never blocks the availability flip. ``user_id`` / ``overlay`` are
    forwarded to the drain for DM targeting and per-overlay bot routing.
    """
    if mode not in VALID_MODES:
        allowed = ", ".join(repr(valid) for valid in sorted(VALID_MODES))
        msg = f"mode must be one of {allowed}, got {mode!r}"
        raise ValueError(msg)
    target = path or override_path()
    # The away→present drain fires when returning to a REACHABLE mode from a
    # deferring one. Keyed on the ``_DEFERRING_MODES`` set (not a bare
    # ``mode == MODE_PRESENT``) so ``away`` and ``autonomous_away`` are handled
    # symmetrically as both source (below, via the set) and — should another
    # reachable mode ever join ``present`` — target. ``present`` is the only
    # non-deferring mode today, so this is behaviour-preserving.
    prior_mode = resolve_mode().mode if mode not in _DEFERRING_MODES else None
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
    if prior_mode in _DEFERRING_MODES:
        _drain_on_return(user_id=user_id, overlay=overlay)
    return target


def _drain_on_return(*, user_id: str, overlay: str) -> None:
    """Auto-fire the away→present deferred-question drain, fail-open.

    Reuses the canonical :func:`teatree.core.notify_question_drains.drain_deferred_questions`
    egress (the same code path ``t3 teatree questions resurface`` runs). Any
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


@dataclass(frozen=True, slots=True)
class UserTurn:
    """A recorded ``UserPromptSubmit`` — when it landed and in which session.

    Carries the session id so the live-turn predicate can tell THIS
    session's fresh prompt apart from a foreign session's (#189). A legacy
    plain-ISO heartbeat (pre-#189) parses with an empty :attr:`session_id`,
    which can therefore never satisfy the same-session check.
    """

    at: datetime
    session_id: str


class PresenceHeartbeat:
    """The durable live-presence signal — a prompt proves the user is here.

    Groups the stamp/read concern so the resolver and the
    ``UserPromptSubmit`` hook share one cohesive seam. The file location is
    injected as :attr:`locate` (the module singleton :data:`PRESENCE`
    resolves it lazily through :func:`presence_path`, so a test repointing
    ``availability.presence_path`` is honoured); a test may also construct a
    heartbeat with an explicit locator.

    The on-disk format is a small JSON document (``{"at": ..., "session":
    ...}``). :meth:`last_seen` still reads a legacy plain-ISO file so a
    heartbeat written before the format gained a session id keeps upgrading
    the schedule.
    """

    def __init__(self, locate: Callable[[], Path] = presence_path) -> None:
        self.locate = locate

    def record(self, *, session_id: str = "", now: datetime | None = None) -> Path:
        """Stamp the heartbeat atomically via ``tmp.replace``.

        Called from the ``UserPromptSubmit`` hook on every genuine user
        prompt. :meth:`last_seen` reads the timestamp (the resolver uses it
        to upgrade a schedule-derived ``away`` to ``present``);
        :meth:`last_user_turn` reads the timestamp plus the session id (the
        #189 live-turn predicate uses both).
        """
        moment = now or datetime.now(tz=UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        target = self.locate()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(prefix=".presence-", suffix=".tmp", dir=str(target.parent))
        tmp_path = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"at": moment.isoformat(), "session": session_id}, fh, sort_keys=True)
                fh.write("\n")
            tmp_path.replace(target)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return target

    def last_seen(self) -> datetime | None:
        """Read the heartbeat timestamp, if present and well-formed.

        A malformed or unreadable stamp returns ``None`` rather than
        raising — the resolver then ignores live presence and falls through
        to the schedule, so a corrupt heartbeat never blocks the user from
        being correctly classified by their cron windows.
        """
        turn = self.last_user_turn()
        return turn.at if turn is not None else None

    def last_user_turn(self) -> UserTurn | None:
        """Read the recorded turn (timestamp + session), if well-formed.

        Tolerates both the JSON format and a legacy plain-ISO file (parsed
        with an empty session id). A malformed or unreadable stamp returns
        ``None`` — the live-turn predicate then treats the turn as not
        user-driven (the safe, deferring default).
        """
        target = self.locate()
        if not target.is_file():
            return None
        try:
            raw = target.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        at, session_id = self._parse(raw)
        if at is None:
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        return UserTurn(at=at, session_id=session_id)

    @staticmethod
    def _parse(raw: str) -> tuple[datetime | None, str]:
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = None
        if isinstance(doc, dict):
            stamp = str(doc.get("at", "")).strip()
            session_id = str(doc.get("session", "")).strip()
            try:
                return datetime.fromisoformat(stamp), session_id
            except ValueError:
                return None, ""
        try:
            return datetime.fromisoformat(raw), ""
        except ValueError:
            return None, ""

    def is_live_user_turn(self, *, session_id: str, now: datetime | None = None) -> bool:
        """True when the user typed a prompt in *session_id* within the live window.

        The #189 user-driven escape: an ``AskUserQuestion`` raised on such a
        turn may render in-client even under away-mode, because the user is
        demonstrably right here, right now. Requires a non-empty *session_id*
        matching the recorded turn's session and a recorded prompt no older
        than :data:`LIVE_TURN_FRESHNESS`. Any missing / foreign-session /
        stale / unparsable signal returns ``False`` — the safe (defer) default
        that keeps BLUEPRINT §17.1 invariant 9 intact for autonomous turns.
        """
        if not session_id:
            return False
        turn = self.last_user_turn()
        if turn is None or turn.session_id != session_id:
            return False
        moment = now or datetime.now(tz=UTC)
        return moment - turn.at <= LIVE_TURN_FRESHNESS

    def refresh_live_turn(self, *, session_id: str, now: datetime | None = None) -> bool:
        """Slide the live-turn window forward for an ALREADY-live same-session turn.

        A multi-question user-driven walk-through (``/checking``) raises
        several ``AskUserQuestion`` calls in one session. The user answering
        one in-client is fresh evidence they are still driving — as strong as
        a new ``UserPromptSubmit``. Re-stamping the heartbeat to *now* keeps
        the next question inside :data:`LIVE_TURN_FRESHNESS`, so an intervening
        background task-notification turn (which never refreshes the heartbeat)
        cannot age the window out mid walk-through (#2058).

        Guarded so it can only ever EXTEND a chain that is already live: it
        re-stamps only when :meth:`is_live_user_turn` currently holds for
        *session_id*. A turn that was never live (an autonomous loop turn), a
        foreign session, or one already aged out is a no-op — so the refresh
        can never fabricate liveness and BLUEPRINT §17.1 invariant 9 stays
        intact for the loop's own questions. Returns ``True`` when the window
        was slid.
        """
        moment = now or datetime.now(tz=UTC)
        if not self.is_live_user_turn(session_id=session_id, now=moment):
            return False
        self.record(session_id=session_id, now=moment)
        return True


PRESENCE = PresenceHeartbeat()


def pending_questions_count(*, using: str | None = None) -> int:
    """Number of unresolved :class:`DeferredQuestion` rows (for statusline)."""
    return DeferredQuestion.pending(using=using).count()


def iter_pending_questions(*, using: str | None = None) -> Iterable[DeferredQuestion]:
    """Yield the unresolved :class:`DeferredQuestion` queue, oldest first."""
    return DeferredQuestion.pending(using=using)


__all__ = [
    "LIVE_TURN_FRESHNESS",
    "MODE_AUTONOMOUS_AWAY",
    "MODE_AWAY",
    "MODE_PRESENT",
    "PRESENCE",
    "PRESENCE_FRESHNESS",
    "VALID_MODES",
    "Override",
    "PresenceHeartbeat",
    "Resolution",
    "Schedule",
    "UserTurn",
    "clear_override",
    "iter_pending_questions",
    "load_override",
    "load_schedule",
    "override_path",
    "pending_questions_count",
    "presence_path",
    "resolve_mode",
    "write_override",
]
