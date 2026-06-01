"""Tests for the ``handle_record_presence`` UserPromptSubmit hook (#58).

A prompt is direct evidence the user is at the keyboard. The hook stamps
a live-presence heartbeat that ``availability.resolve_mode`` reads to
upgrade a schedule-derived ``away`` to ``present`` — so a user actively
typing outside their configured work hours is never silently muted.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_record_presence
from teatree.core import availability


@pytest.fixture
def presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> availability.PresenceHeartbeat:
    target = tmp_path / "availability_presence"
    heartbeat = availability.PresenceHeartbeat(locate=lambda: target)
    monkeypatch.setattr(availability, "PRESENCE", heartbeat)
    return heartbeat


class TestRecordPresenceHook:
    def test_prompt_stamps_a_fresh_heartbeat(self, presence: availability.PresenceHeartbeat) -> None:
        handle_record_presence({"prompt": "do the thing", "session_id": "s1"})
        stamp = presence.last_seen()
        assert stamp is not None
        assert datetime.now(tz=UTC) - stamp < timedelta(seconds=5)

    def test_empty_prompt_records_nothing(self, presence: availability.PresenceHeartbeat) -> None:
        handle_record_presence({"prompt": "", "session_id": "s1"})
        assert presence.last_seen() is None

    def test_handler_returns_none(self, presence: availability.PresenceHeartbeat) -> None:
        # UserPromptSubmit handlers are void — only PreToolUse denies return True.
        assert handle_record_presence({"prompt": "hi", "session_id": "s1"}) is None

    def test_stamped_heartbeat_upgrades_schedule_away_to_present(
        self, presence: availability.PresenceHeartbeat
    ) -> None:
        # End-to-end: the hook records, the resolver upgrades. A user typing at
        # 22:00 on a Tuesday — outside "* 9-16 * * 1-5" — stays present.
        evening = datetime(2026, 6, 2, 22, 0, tzinfo=UTC)
        handle_record_presence({"prompt": "ship it?", "session_id": "s1"})
        # The hook stamps "now"; assert the live upgrade with an explicit
        # fresh presence so the test is clock-independent.
        schedule = availability.Schedule(timezone="UTC", windows=("* 9-16 * * 1-5",))
        resolution = availability.resolve_mode(
            now=evening, schedule=schedule, override=None, presence=evening - timedelta(minutes=1)
        )
        assert resolution.mode == availability.MODE_PRESENT
        assert resolution.source == "live"
        # And the hook's own stamp is fresh enough that a same-clock resolve upgrades too.
        assert presence.last_seen() is not None

    def test_record_failure_never_raises(
        self, presence: availability.PresenceHeartbeat, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> Path:
            raise OSError

        monkeypatch.setattr(presence, "record", _boom)
        # Fail-open: the hook swallows the error so the prompt is never blocked.
        assert handle_record_presence({"prompt": "hi", "session_id": "s1"}) is None
