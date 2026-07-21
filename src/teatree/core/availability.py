"""Live-presence heartbeat + the fast-hook availability-mirror primitives (#58, #61).

Post-merge the operating mode is resolved by
:func:`teatree.core.mode_resolution.resolve_active_mode` over the unified
:class:`~teatree.core.models.Mode` — the old standalone availability resolver
(``resolve_mode`` + the cron ``Schedule`` / on-disk ``Override`` substrate) is gone.
What survives here are the two primitives the merge still needs:

*   **The live-presence heartbeat** (:class:`PresenceHeartbeat` / :data:`PRESENCE`)
    — a ``UserPromptSubmit`` proves the user is at the keyboard now. The resolver
    reads :meth:`PresenceHeartbeat.last_seen` for its presence-sensitivity upgrade
    (a fresh keystroke within :data:`PRESENCE_FRESHNESS` beats a scheduled away
    mode), and the #189 per-turn escape reads :meth:`is_live_user_turn` (within the
    shorter :data:`LIVE_TURN_FRESHNESS`). This is an INPUT to mode resolution, not a
    mode.
*   **The fast-hook override mirror** (:func:`override_path` / :func:`clear_override`
    + the legacy ``MODE_*`` string tokens) — the stdlib away-probe
    (``hooks/scripts/availability_away_probe.py``) gates AskUserQuestion deferral /
    the self-pump pause WITHOUT a Django boot, so it reads a mirrored
    ``availability_override.json``. The merged-mode override chokepoint
    (:func:`teatree.core.mode_resolution.set_mode_override`) write-throughs the
    resolved posture there; the DB ``ModeOverride`` row stays authoritative for every
    Django consumer.

The presence + mirror files are written via ``tmp.replace`` (atomic) so a torn
write never leaves a half-encoded document; readers tolerating a read race
re-resolve cleanly.
"""

import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.paths import DATA_DIR

logger = logging.getLogger(__name__)

# The legacy availability-mode string tokens the fast-hook away-probe still reads
# from the mirrored ``availability_override.json`` (#61). Post-merge these are only
# the tokens :func:`teatree.core.mode_resolution._legacy_token` maps a resolved
# mode's booleans to when write-through-mirroring the posture for the bare hooks.
MODE_PRESENT = "present"
MODE_AWAY = "away"
MODE_AUTONOMOUS_AWAY = "autonomous_away"

# How recently a ``UserPromptSubmit`` must have landed for the user to count
# as demonstrably present. A live prompt within this window upgrades a
# schedule-derived away-class mode to a present-class one (the resolver's
# presence-sensitivity rule) — long enough to bridge a normal pause between
# prompts, short enough that a user who walked away an hour ago is treated as away.
PRESENCE_FRESHNESS = timedelta(minutes=15)

# How recently a ``UserPromptSubmit`` must have landed, IN THIS SESSION, for the
# current turn to count as user-driven (#189). Intentionally far shorter than
# :data:`PRESENCE_FRESHNESS`: this is "the user typed THIS turn", not "the user
# is reachable today". It must outlive the few tool calls between the prompt and
# the agent's ``AskUserQuestion`` reply, but expire long before a follow-on
# autonomous turn could be mistaken for a fresh keystroke.
LIVE_TURN_FRESHNESS = timedelta(seconds=90)


def override_path() -> Path:
    """Location of the fast-hook availability-override mirror JSON file."""
    return DATA_DIR / "availability_override.json"


def presence_path() -> Path:
    """Location of the durable live-presence heartbeat file."""
    return DATA_DIR / "availability_presence"


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
    "PresenceHeartbeat",
    "UserTurn",
    "clear_override",
    "iter_pending_questions",
    "override_path",
    "pending_questions_count",
    "presence_path",
]
