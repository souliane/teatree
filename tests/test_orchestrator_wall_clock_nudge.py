"""Wall-clock responsiveness dimension of the orchestrator nudge (#1733 §2).

The count-based nudge (#1727) steers the orchestrator to yield once a turn
crosses N tool calls. That misses the SLOW-but-few-calls failure: a handful
of long-blocking calls can tie the session up for minutes without ever
crossing the tool-call budget. #1733 §2 adds a WALL-CLOCK dimension alongside
the count one: once the elapsed wall-clock since the last user-visible action
(the turn start) exceeds a configurable threshold, the same yield nudge fires
— independent of how many tool calls have been made.

Both dimensions are advisory (never a deny), config-driven, fail-open, and
reset every user turn. These tests exercise the two dimensions independently.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _orchestrator_turn_wall_clock_threshold,
    handle_orchestrator_turn_budget_nudge,
    handle_reset_turn_tool_budget,
)


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "STATE_DIR", tmp_path / "state")
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _clean_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))


def _write_toml(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    home = Path.home()
    (home / ".teatree.toml").write_text(body, encoding="utf-8")


def _bash(session_id: str, command: str = "git status") -> dict:
    return {"session_id": session_id, "tool_name": "Bash", "tool_input": {"command": command}}


def _set_turn_start(session_id: str, monotonic_value: float) -> None:
    """Force the turn-start timestamp file so elapsed wall-clock is deterministic."""
    (router.STATE_DIR / f"{session_id}.{router._TURN_START_SUFFIX}").write_text(str(monotonic_value), encoding="utf-8")


class TestWallClockThresholdConfig:
    def test_default_threshold_is_positive(self) -> None:
        assert _orchestrator_turn_wall_clock_threshold() == router._DEFAULT_ORCHESTRATOR_WALL_CLOCK_SECONDS

    def test_explicit_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_toml(monkeypatch, "[teatree]\norchestrator_turn_wall_clock_seconds = 0\n")
        assert _orchestrator_turn_wall_clock_threshold() == 0

    def test_explicit_value_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_toml(monkeypatch, "[teatree]\norchestrator_turn_wall_clock_seconds = 42\n")
        assert _orchestrator_turn_wall_clock_threshold() == 42

    def test_broken_config_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_toml(monkeypatch, "not valid = toml [[[")
        assert _orchestrator_turn_wall_clock_threshold() == router._DEFAULT_ORCHESTRATOR_WALL_CLOCK_SECONDS


class TestWallClockDimensionFiresIndependentOfCount:
    """The wall-clock nudge fires on elapsed TIME, not tool-call count."""

    def test_nudge_fires_when_elapsed_exceeds_threshold_with_few_calls(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Threshold 60s; count budget high so the count dimension never fires.
        _write_toml(
            monkeypatch,
            "[teatree]\norchestrator_turn_budget = 1000\norchestrator_turn_wall_clock_seconds = 60\n",
        )
        sid = "sess-wall-fire"
        # Pin the clock: turn started long ago.
        monkeypatch.setattr(router.time, "monotonic", lambda: 1000.0)
        _set_turn_start(sid, 900.0)  # 100s elapsed > 60s threshold
        handle_orchestrator_turn_budget_nudge(_bash(sid))
        out = capsys.readouterr().out
        assert out.strip(), "wall-clock nudge must emit additionalContext when elapsed exceeds the threshold"
        payload = json.loads(out)
        assert "responsiveness" in payload["additionalContext"].lower()

    def test_no_nudge_when_elapsed_below_threshold(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_toml(
            monkeypatch,
            "[teatree]\norchestrator_turn_budget = 1000\norchestrator_turn_wall_clock_seconds = 60\n",
        )
        sid = "sess-wall-quiet"
        monkeypatch.setattr(router.time, "monotonic", lambda: 1000.0)
        _set_turn_start(sid, 980.0)  # 20s elapsed < 60s threshold
        handle_orchestrator_turn_budget_nudge(_bash(sid))
        assert capsys.readouterr().out.strip() == "", "no nudge before the wall-clock threshold is crossed"

    def test_wall_clock_disabled_when_threshold_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_toml(
            monkeypatch,
            "[teatree]\norchestrator_turn_budget = 1000\norchestrator_turn_wall_clock_seconds = 0\n",
        )
        sid = "sess-wall-off"
        monkeypatch.setattr(router.time, "monotonic", lambda: 1_000_000.0)
        _set_turn_start(sid, 0.0)  # huge elapsed, but dimension disabled
        handle_orchestrator_turn_budget_nudge(_bash(sid))
        assert capsys.readouterr().out.strip() == "", "threshold 0 disables the wall-clock dimension"

    def test_wall_clock_nudge_emitted_once_per_turn(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_toml(
            monkeypatch,
            "[teatree]\norchestrator_turn_budget = 1000\norchestrator_turn_wall_clock_seconds = 30\n",
        )
        sid = "sess-wall-once"
        monkeypatch.setattr(router.time, "monotonic", lambda: 500.0)
        _set_turn_start(sid, 400.0)  # 100s elapsed
        handle_orchestrator_turn_budget_nudge(_bash(sid))
        first = capsys.readouterr().out.strip()
        assert first, "first crossing nudges"
        handle_orchestrator_turn_budget_nudge(_bash(sid))
        assert capsys.readouterr().out.strip() == "", "the nudge is idempotent within a turn"

    def test_subagent_call_never_nudged_by_wall_clock(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_toml(monkeypatch, "[teatree]\norchestrator_turn_wall_clock_seconds = 30\n")
        sid = "sess-wall-sub"
        monkeypatch.setattr(router.time, "monotonic", lambda: 500.0)
        _set_turn_start(sid, 0.0)
        data = _bash(sid)
        data["agent_id"] = "sub-1"
        handle_orchestrator_turn_budget_nudge(data)
        assert capsys.readouterr().out.strip() == "", "sub-agents are exempt from the responsiveness nudge"


class TestCountDimensionStillFiresIndependently:
    """The count-based dimension keeps working with the wall-clock dimension off."""

    def test_count_nudge_fires_with_wall_clock_disabled(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Wall-clock disabled (0); a short count budget still nudges on call count.
        _write_toml(
            monkeypatch,
            "[teatree]\norchestrator_turn_budget = 2\norchestrator_turn_wall_clock_seconds = 0\n",
        )
        sid = "sess-count-only"
        # Turn started "now" so no wall-clock elapse.
        monkeypatch.setattr(router.time, "monotonic", lambda: 1000.0)
        _set_turn_start(sid, 1000.0)
        handle_orchestrator_turn_budget_nudge(_bash(sid))  # count 1, below budget
        assert capsys.readouterr().out.strip() == ""
        handle_orchestrator_turn_budget_nudge(_bash(sid))  # count 2, hits budget
        out = capsys.readouterr().out.strip()
        assert out, "count dimension must still fire when the wall-clock dimension is off"


class TestTurnResetReArmsBothDimensions:
    def test_reset_clears_turn_start_and_nudge_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sid = "sess-reset"
        _set_turn_start(sid, 123.0)
        (router.STATE_DIR / f"{sid}.{router._TURN_NUDGED_SUFFIX}").write_text("1", encoding="utf-8")
        handle_reset_turn_tool_budget({"session_id": sid})
        assert not (router.STATE_DIR / f"{sid}.{router._TURN_START_SUFFIX}").exists()
        assert not (router.STATE_DIR / f"{sid}.{router._TURN_NUDGED_SUFFIX}").exists()
