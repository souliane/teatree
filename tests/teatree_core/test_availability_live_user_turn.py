"""Tests for the live-user-turn predicate — the #189 user-driven escape.

``availability.is_live_user_turn`` answers a narrow question: did the user
type a prompt in THIS session within the last few seconds (this turn)?
A user-driven turn lets the away-mode ``AskUserQuestion`` hook render the
question LIVE even under a manual-away override, while a loop-driven /
stale turn keeps deferring (BLUEPRINT §17.1 invariant 9 unweakened).

The window is intentionally short (seconds), distinct from the 15-minute
schedule-upgrade ``PRESENCE_FRESHNESS``: a prompt minutes ago is no longer
"this turn". The predicate is fail-safe — a missing, foreign-session, or
unparsable stamp is NOT a live user turn (the conservative, defer path).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from teatree.core import availability
from teatree.core.availability import LIVE_TURN_FRESHNESS, PRESENCE_FRESHNESS, PresenceHeartbeat, is_live_user_turn


@pytest.fixture
def presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PresenceHeartbeat:
    target = tmp_path / "availability_presence"
    heartbeat = PresenceHeartbeat(locate=lambda: target)
    monkeypatch.setattr(availability, "PRESENCE", heartbeat)
    return heartbeat


class TestLiveTurnWindow:
    def test_window_is_short_seconds_not_the_schedule_freshness(self) -> None:
        assert timedelta(minutes=2) > LIVE_TURN_FRESHNESS
        assert LIVE_TURN_FRESHNESS < PRESENCE_FRESHNESS

    def test_fresh_same_session_prompt_is_a_live_turn(self, presence: PresenceHeartbeat) -> None:
        now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
        presence.record(session_id="sess-a", now=now)
        assert is_live_user_turn(session_id="sess-a", now=now + timedelta(seconds=2)) is True

    def test_prompt_minutes_ago_is_not_this_turn(self, presence: PresenceHeartbeat) -> None:
        now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
        presence.record(session_id="sess-a", now=now)
        # Within the 15-min schedule freshness, but well past the this-turn window.
        assert is_live_user_turn(session_id="sess-a", now=now + timedelta(minutes=5)) is False

    def test_fresh_prompt_from_a_different_session_is_not_this_turn(self, presence: PresenceHeartbeat) -> None:
        now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
        presence.record(session_id="sess-a", now=now)
        assert is_live_user_turn(session_id="sess-b", now=now + timedelta(seconds=2)) is False

    def test_no_stamp_is_not_a_live_turn(self, presence: PresenceHeartbeat) -> None:
        assert is_live_user_turn(session_id="sess-a", now=datetime.now(tz=UTC)) is False

    def test_empty_session_id_is_not_a_live_turn(self, presence: PresenceHeartbeat) -> None:
        now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
        presence.record(session_id="sess-a", now=now)
        assert is_live_user_turn(session_id="", now=now + timedelta(seconds=1)) is False

    def test_corrupt_stamp_is_not_a_live_turn(self, presence: PresenceHeartbeat) -> None:
        presence.locate().parent.mkdir(parents=True, exist_ok=True)
        presence.locate().write_text("not json, not iso\n", encoding="utf-8")
        assert is_live_user_turn(session_id="sess-a", now=datetime.now(tz=UTC)) is False


class TestHeartbeatRecordsSession:
    def test_record_persists_session_and_timestamp(self, presence: PresenceHeartbeat) -> None:
        now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
        presence.record(session_id="sess-x", now=now)
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.session_id == "sess-x"
        assert turn.at == now

    def test_last_seen_still_reads_the_new_format_for_schedule_upgrade(self, presence: PresenceHeartbeat) -> None:
        # The 15-min schedule-upgrade path reads ``last_seen`` — it must keep
        # working after the file format gains a session_id.
        now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
        presence.record(session_id="sess-x", now=now)
        assert presence.last_seen() == now

    def test_last_seen_tolerates_legacy_plain_iso_file(self, presence: PresenceHeartbeat) -> None:
        # A heartbeat written by the pre-#189 plain-ISO format must still
        # upgrade the schedule (back-compat across the format change).
        legacy = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
        presence.locate().parent.mkdir(parents=True, exist_ok=True)
        presence.locate().write_text(legacy.isoformat() + "\n", encoding="utf-8")
        assert presence.last_seen() == legacy

    def test_legacy_plain_iso_file_has_no_user_turn_session(self, presence: PresenceHeartbeat) -> None:
        # A legacy stamp carries no session id, so it can never satisfy the
        # same-session live-turn predicate — fail-safe to deferring.
        legacy = datetime.now(tz=UTC)
        presence.locate().parent.mkdir(parents=True, exist_ok=True)
        presence.locate().write_text(legacy.isoformat() + "\n", encoding="utf-8")
        turn = presence.last_user_turn()
        assert turn is None or turn.session_id == ""
        assert is_live_user_turn(session_id="sess-a", now=legacy + timedelta(seconds=1)) is False
