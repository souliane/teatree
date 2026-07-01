"""Autonomous-away availability state (#2544).

A permanent holiday-``away`` override silently kills long unattended runs:
``away`` both defers questions (wanted) and pauses the self-pump (unwanted for
an unattended operator). ``autonomous_away`` splits the two behaviours — defer
questions like ``away``, keep self-pumping like ``present``.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from teatree.core import availability
from teatree.core.availability import (
    MODE_AUTONOMOUS_AWAY,
    MODE_AWAY,
    MODE_PRESENT,
    Resolution,
    resolve_mode,
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
