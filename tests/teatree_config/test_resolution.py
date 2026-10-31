"""A fallback value comes from the declaration that states it, never from a literal beside the reader."""

from unittest.mock import MagicMock, patch

import pytest

from teatree.config import cadence_seconds

_RESOLUTION = "teatree.config.resolution"


def _declaring(seconds: int) -> MagicMock:
    return MagicMock(return_value=MagicMock(loop_cadence_seconds=seconds))


def test_a_malformed_cadence_env_falls_back_to_the_declared_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_LOOP_CADENCE", "garbage")

    with patch(f"{_RESOLUTION}.UserSettings", _declaring(900)):
        assert cadence_seconds() == 900


def test_the_declared_fallback_keeps_the_sixty_second_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_LOOP_CADENCE", "garbage")

    with patch(f"{_RESOLUTION}.UserSettings", _declaring(10)):
        assert cadence_seconds() == 60


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_cadence_env_falls_through_to_the_stored_setting(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv("T3_LOOP_CADENCE", blank)

    with (
        patch(f"{_RESOLUTION}.UserSettings", _declaring(900)),
        patch(f"{_RESOLUTION}.get_effective_settings", return_value=MagicMock(loop_cadence_seconds=300)),
    ):
        assert cadence_seconds() == 300
