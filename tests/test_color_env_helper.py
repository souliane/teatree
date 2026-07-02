"""Tests for the shared color-forcing-hermetic subprocess env builder.

The contract that matters (souliane/teatree#2359): :func:`no_color_env` strips
``FORCE_COLOR``/``CLICOLOR_FORCE``/``CLICOLORS`` regardless of whether the
ambient shell set them, so a subprocess spawned with it never emits ANSI SGR
codes a test's plain-text regex/substring match cannot straddle.
"""

import pytest

from tests._color_env import no_color_env


class TestNoColorEnv:
    def test_strips_force_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FORCE_COLOR", "3")
        assert "FORCE_COLOR" not in no_color_env()

    def test_strips_clicolor_force(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLICOLOR_FORCE", "1")
        assert "CLICOLOR_FORCE" not in no_color_env()

    def test_strips_clicolors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLICOLORS", "1")
        assert "CLICOLORS" not in no_color_env()

    def test_sets_no_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert no_color_env()["NO_COLOR"] == "1"

    def test_noop_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
        monkeypatch.delenv("CLICOLORS", raising=False)
        env = no_color_env()  # must not raise
        assert "FORCE_COLOR" not in env

    def test_preserves_other_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
        assert no_color_env()["SOME_OTHER_VAR"] == "keep-me"
