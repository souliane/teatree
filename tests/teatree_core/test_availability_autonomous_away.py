"""Autonomous-away availability state (#2544).

A permanent holiday-``away`` override silently kills long unattended runs:
``away`` both defers questions (wanted) and pauses the self-pump (unwanted for
an unattended operator). ``autonomous_away`` splits the two behaviours — defer
questions like ``away``, keep self-pumping like ``present``.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from teatree.core import availability
from teatree.core.availability import (
    MODE_AUTONOMOUS_AWAY,
    MODE_AWAY,
    MODE_PRESENT,
    Override,
    Resolution,
    override_set_at,
    resolve_mode,
    stale_override_finding,
    write_override,
)


def _defers(mode: str) -> bool:
    return Resolution(mode=mode, source="override").defers_questions


def _pauses(mode: str) -> bool:
    return Resolution(mode=mode, source="override").pauses_self_pump


@pytest.fixture
def override_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "availability_override.json"
    monkeypatch.setattr("teatree.core.availability.override_path", lambda: target)
    return target


class TestModePredicates:
    def test_present_neither_defers_nor_pauses(self) -> None:
        assert not _defers(MODE_PRESENT)
        assert not _pauses(MODE_PRESENT)

    def test_away_defers_and_pauses(self) -> None:
        assert _defers(MODE_AWAY)
        assert _pauses(MODE_AWAY)

    def test_autonomous_away_defers_but_does_not_pause(self) -> None:
        # The whole point of #2544: questions defer yet the factory keeps running.
        assert _defers(MODE_AUTONOMOUS_AWAY)
        assert not _pauses(MODE_AUTONOMOUS_AWAY)

    def test_unknown_mode_neither(self) -> None:
        assert not _defers("garbage")
        assert not _pauses("garbage")


class TestAutonomousAwayOverride:
    def test_override_resolves_to_autonomous_away(self, override_file: Path) -> None:
        write_override(MODE_AUTONOMOUS_AWAY)
        resolution = resolve_mode()
        assert resolution.mode == MODE_AUTONOMOUS_AWAY
        assert resolution.source == "override"

    def test_autonomous_away_is_a_valid_persisted_override(self, override_file: Path) -> None:
        write_override(MODE_AUTONOMOUS_AWAY)
        loaded = availability.load_override()
        assert loaded is not None
        assert loaded.mode == MODE_AUTONOMOUS_AWAY
        assert loaded.is_active(datetime.now(tz=UTC))


class TestReturnFromAutonomousAwayDrains:
    def test_autonomous_away_to_present_drains_backlog(
        self, override_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Returning from autonomous-away must resurface deferred questions just
        # like returning from holiday-away — both deferred the user's questions.
        write_override(MODE_AUTONOMOUS_AWAY)
        drained: list[tuple[str, str]] = []
        monkeypatch.setattr(
            availability,
            "drain_deferred_questions",
            lambda *, user_id, overlay: drained.append((user_id, overlay)),
        )
        write_override(MODE_PRESENT, user_id="U1", overlay="ov")
        assert drained == [("U1", "ov")]

    def test_present_to_present_does_not_drain(self, override_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        write_override(MODE_PRESENT)
        drained: list[object] = []
        monkeypatch.setattr(
            availability,
            "drain_deferred_questions",
            lambda *, user_id, overlay: drained.append(object()),
        )
        write_override(MODE_PRESENT)
        assert drained == []


class TestOverrideSetAt:
    """`override_set_at` reads the override file mtime — the "how long active" signal (#3274)."""

    def test_returns_the_file_mtime_when_present(self, override_file: Path) -> None:
        write_override(MODE_AUTONOMOUS_AWAY)
        set_at = override_set_at()
        assert set_at is not None
        assert abs((datetime.now(tz=UTC) - set_at).total_seconds()) < 60

    def test_returns_none_when_absent(self, override_file: Path) -> None:
        assert override_set_at() is None


class TestStaleOverrideFinding:
    """#3274: `t3 doctor` flags a no-expiry deferring override that has outlived the threshold."""

    _NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    _LOOPS = ("review", "followup")

    def _finding(self, override: Override | None, *, set_at: datetime | None) -> str | None:
        return stale_override_finding(
            override=override,
            set_at=set_at,
            now=self._NOW,
            colleague_facing_loops=self._LOOPS,
        )

    def test_old_no_expiry_autonomous_away_is_flagged(self) -> None:
        override = Override(mode=MODE_AUTONOMOUS_AWAY, until=None)
        msg = self._finding(override, set_at=self._NOW - timedelta(hours=30))
        assert msg is not None
        assert "autonomous_away" in msg
        assert "followup, review" in msg
        assert "t3 teatree availability auto" in msg

    def test_old_no_expiry_away_notes_self_pump_pause(self) -> None:
        override = Override(mode=MODE_AWAY, until=None)
        msg = self._finding(override, set_at=self._NOW - timedelta(hours=30))
        assert msg is not None
        assert "self-pump" in msg

    def test_recent_override_is_not_flagged(self) -> None:
        override = Override(mode=MODE_AUTONOMOUS_AWAY, until=None)
        assert self._finding(override, set_at=self._NOW - timedelta(hours=1)) is None

    def test_bounded_override_is_not_flagged(self) -> None:
        override = Override(mode=MODE_AUTONOMOUS_AWAY, until=self._NOW + timedelta(hours=48))
        assert self._finding(override, set_at=self._NOW - timedelta(hours=30)) is None

    def test_present_override_is_not_flagged(self) -> None:
        override = Override(mode=MODE_PRESENT, until=None)
        assert self._finding(override, set_at=self._NOW - timedelta(hours=30)) is None

    def test_no_override_is_not_flagged(self) -> None:
        assert self._finding(None, set_at=self._NOW - timedelta(hours=30)) is None

    def test_missing_set_at_is_not_flagged(self) -> None:
        override = Override(mode=MODE_AUTONOMOUS_AWAY, until=None)
        assert self._finding(override, set_at=None) is None
