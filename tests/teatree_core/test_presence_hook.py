"""Tests for the ``handle_record_presence`` UserPromptSubmit hook (#58).

A prompt is direct evidence the user is at the keyboard. The hook stamps
a live-presence heartbeat that ``availability.resolve_mode`` reads to
upgrade a schedule-derived ``away`` to ``present`` — so a user actively
typing outside their configured work hours is never silently muted.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hooks.scripts.hook_router import _LOOP_PROMPT, _is_live_user_turn, handle_record_presence
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


class TestPresenceStampsSessionForLiveTurn:
    """The heartbeat carries the session id so the live-turn predicate works.

    It must tell THIS session's fresh prompt apart from a foreign one (#189).
    A loop-tick prompt is autonomous, not user presence — it must NOT stamp.
    """

    def test_user_prompt_stamps_the_session(self, presence: availability.PresenceHeartbeat) -> None:
        handle_record_presence({"prompt": "do the thing", "session_id": "s1"})
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.session_id == "s1"

    def test_loop_tick_prompt_does_not_stamp_presence(self, presence: availability.PresenceHeartbeat) -> None:
        handle_record_presence({"prompt": _LOOP_PROMPT, "session_id": "owner"})
        assert presence.last_user_turn() is None
        assert presence.last_seen() is None

    def test_bare_loop_prompt_with_harness_ambient_does_not_stamp(
        self, presence: availability.PresenceHeartbeat
    ) -> None:
        # A pure cron tick still reduces to the bare loop prompt after the
        # harness-injected <system-reminder> ambient blocks are stripped, so it
        # must NOT stamp — invariant 9 holds for the autonomous tick.
        prompt = _LOOP_PROMPT + "\n<system-reminder>CLAUDE.md body…</system-reminder>"
        handle_record_presence({"prompt": prompt, "session_id": "owner"})
        assert presence.last_user_turn() is None
        assert presence.last_seen() is None


class TestFreshUserPromptDuringLoopStampsPresence:
    """#2155: a fresh user prompt interleaved with a loop continuation stamps.

    The reported high-irritation bug: the loop owner is self-pumping; the user
    types a genuine fresh prompt that the harness delivers PREFIXED by the loop
    continuation text. The old guard suppressed recording for ANY prompt that
    merely ``startswith(_LOOP_PROMPT)``, so the user's live keystroke was
    swallowed — and the next ``AskUserQuestion`` deferred to Slack as a
    ``DeferredQuestion`` even though the user was demonstrably present. The fix
    suppresses ONLY the BARE loop prompt (after ambient strip): any genuine
    user-authored content beyond it proves presence and must stamp.
    """

    def test_loop_prefixed_prompt_with_user_text_stamps(self, presence: availability.PresenceHeartbeat) -> None:
        prompt = _LOOP_PROMPT + "\n\nactually, hold off and check #2111 first"
        handle_record_presence({"prompt": prompt, "session_id": "owner"})
        turn = presence.last_user_turn()
        assert turn is not None, "a fresh user prompt prefixed by the loop text must still stamp"
        assert turn.session_id == "owner"

    def test_loop_prefixed_user_prompt_makes_the_turn_live(self, presence: availability.PresenceHeartbeat) -> None:
        # End-to-end through the seam: stamping makes the live-turn predicate true,
        # so a same-session AskUserQuestion on this turn renders in-client.
        prompt = _LOOP_PROMPT + "\n\nwhich option do you prefer, A or B?"
        handle_record_presence({"prompt": prompt, "session_id": "owner"})
        assert _is_live_user_turn({"session_id": "owner"}) is True


class TestIsLiveUserTurnHookPredicate:
    """The hook-side ``_is_live_user_turn`` wraps the availability predicate.

    Crash-proof: it bootstraps Django, reads the heartbeat, and fails SAFE
    (False => defer) on any error or missing signal.
    """

    def test_returns_true_for_fresh_same_session_prompt(self, presence: availability.PresenceHeartbeat) -> None:
        handle_record_presence({"prompt": "approve?", "session_id": "s-live"})
        assert _is_live_user_turn({"session_id": "s-live"}) is True

    def test_returns_false_when_no_recent_prompt(self, presence: availability.PresenceHeartbeat) -> None:
        assert _is_live_user_turn({"session_id": "s-live"}) is False

    def test_returns_false_for_foreign_session(self, presence: availability.PresenceHeartbeat) -> None:
        handle_record_presence({"prompt": "approve?", "session_id": "s-live"})
        assert _is_live_user_turn({"session_id": "s-other"}) is False

    def test_fails_safe_when_predicate_raises(
        self, presence: availability.PresenceHeartbeat, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(**_kwargs: object) -> bool:
            msg = "boom"
            raise RuntimeError(msg)

        monkeypatch.setattr(presence, "is_live_user_turn", _boom)
        assert _is_live_user_turn({"session_id": "s-live"}) is False
