"""Tests for teatree.agents.handover — agent handover config, telemetry, threshold logic."""

import json
from pathlib import Path

from django.test import override_settings

from teatree.agents.handover import (
    _get_next_runtime,
    _get_preferred_runtime,
    _get_runtime_policy,
    _get_switch_threshold,
    build_claude_handover_status,
    get_agent_handover_config,
    get_claude_statusline_state_dir,
    get_claude_telemetry_path,
    get_recommended_runtime,
    load_claude_telemetry,
    should_suggest_handover,
)

_TWO_RUNTIME_CONFIG = [
    {
        "runtime": "claude-code",
        "telemetry": {
            "provider": "claude-statusline",
            "switch_away_at_percent": 95,
            "switch_back_at_percent": 80,
        },
    },
    {"runtime": "codex"},
]

_SINGLE_RUNTIME_CONFIG = [
    {
        "runtime": "claude-code",
        "telemetry": {
            "provider": "claude-statusline",
            "switch_away_at_percent": 95,
            "switch_back_at_percent": 80,
        },
    },
]

# --- get_claude_statusline_state_dir ---


def test_state_dir_explicit_path() -> None:
    result = get_claude_statusline_state_dir(state_dir="/custom/path")
    assert result == Path("/custom/path")


def test_state_dir_explicit_str() -> None:
    result = get_claude_statusline_state_dir(state_dir="/another/path")
    assert isinstance(result, Path)
    assert str(result) == "/another/path"


def test_state_dir_none_uses_default() -> None:
    result = get_claude_statusline_state_dir(state_dir=None)
    assert result == Path("/tmp/claude-statusline")


@override_settings(TEATREE_CLAUDE_STATUSLINE_STATE_DIR="/configured/dir")
def test_state_dir_reads_settings() -> None:
    result = get_claude_statusline_state_dir(state_dir=None)
    assert result == Path("/configured/dir")


# --- get_agent_handover_config ---


def test_default_handover_config() -> None:
    config = get_agent_handover_config()
    assert len(config) == 1
    assert config[0]["runtime"] == "claude-code"
    assert "telemetry" in config[0]


@override_settings(TEATREE_AGENT_HANDOVER="not-a-list")
def test_handover_config_non_list_returns_default() -> None:
    config = get_agent_handover_config()
    assert config[0]["runtime"] == "claude-code"


@override_settings(TEATREE_AGENT_HANDOVER=[{"runtime": "my-agent"}])
def test_handover_config_custom() -> None:
    config = get_agent_handover_config()
    assert len(config) == 1
    assert config[0]["runtime"] == "my-agent"


@override_settings(TEATREE_AGENT_HANDOVER=["not-a-dict", {"runtime": "ok"}])
def test_handover_config_skips_non_dict_items() -> None:
    config = get_agent_handover_config()
    assert len(config) == 1
    assert config[0]["runtime"] == "ok"


@override_settings(TEATREE_AGENT_HANDOVER=[{"runtime": ""}, {"runtime": "ok"}])
def test_handover_config_skips_empty_runtime() -> None:
    config = get_agent_handover_config()
    assert len(config) == 1
    assert config[0]["runtime"] == "ok"


@override_settings(TEATREE_AGENT_HANDOVER=[{"runtime": 123}])
def test_handover_config_skips_non_string_runtime() -> None:
    """Non-string runtime falls back to default."""
    config = get_agent_handover_config()
    assert config[0]["runtime"] == "claude-code"


@override_settings(TEATREE_AGENT_HANDOVER=[{"no_runtime_key": True}])
def test_handover_config_skips_missing_runtime_key() -> None:
    config = get_agent_handover_config()
    assert config[0]["runtime"] == "claude-code"


@override_settings(
    TEATREE_AGENT_HANDOVER=[
        {"runtime": "agent-a", "telemetry": {"switch_away_at_percent": 90}},
    ]
)
def test_handover_config_preserves_telemetry() -> None:
    config = get_agent_handover_config()
    assert config[0]["telemetry"] == {"switch_away_at_percent": 90}


@override_settings(TEATREE_AGENT_HANDOVER=[{"runtime": "agent-a", "telemetry": "not-a-dict"}])
def test_handover_config_ignores_non_dict_telemetry() -> None:
    config = get_agent_handover_config()
    assert "telemetry" not in config[0]


@override_settings(TEATREE_AGENT_HANDOVER=[])
def test_handover_config_empty_list_returns_default() -> None:
    """Empty normalized list falls back to default."""
    config = get_agent_handover_config()
    assert config[0]["runtime"] == "claude-code"


# --- _get_runtime_policy ---


def test_get_runtime_policy_found() -> None:
    policy = _get_runtime_policy("claude-code")
    assert policy["runtime"] == "claude-code"
    assert "telemetry" in policy


def test_get_runtime_policy_not_found() -> None:
    policy = _get_runtime_policy("nonexistent")
    assert policy == {}


# --- _get_next_runtime ---


@override_settings(TEATREE_AGENT_HANDOVER=_TWO_RUNTIME_CONFIG)
def test_get_next_runtime_exists() -> None:
    assert _get_next_runtime("claude-code") == "codex"


@override_settings(TEATREE_AGENT_HANDOVER=_TWO_RUNTIME_CONFIG)
def test_get_next_runtime_last_in_list() -> None:
    assert _get_next_runtime("codex") == ""


@override_settings(TEATREE_AGENT_HANDOVER=_SINGLE_RUNTIME_CONFIG)
def test_get_next_runtime_single_config() -> None:
    """Single-runtime config has no next runtime."""
    assert _get_next_runtime("claude-code") == ""


def test_get_next_runtime_unknown() -> None:
    assert _get_next_runtime("nonexistent") == ""


# --- _get_preferred_runtime ---


def test_get_preferred_runtime() -> None:
    assert _get_preferred_runtime() == "claude-code"


@override_settings(TEATREE_AGENT_HANDOVER=[])
def test_get_preferred_runtime_empty_config() -> None:
    """Empty config (all items invalid) falls back to default, so preferred is claude-code."""
    # The empty list triggers the default fallback
    assert _get_preferred_runtime() == "claude-code"


# --- _get_switch_threshold ---


def test_switch_threshold_valid() -> None:
    threshold = _get_switch_threshold("claude-code", "switch_away_at_percent")
    assert threshold == 95


@override_settings(TEATREE_AGENT_HANDOVER=_TWO_RUNTIME_CONFIG)
def test_switch_threshold_no_telemetry() -> None:
    threshold = _get_switch_threshold("codex", "switch_away_at_percent")
    assert threshold is None


def test_switch_threshold_unknown_runtime() -> None:
    threshold = _get_switch_threshold("nonexistent", "switch_away_at_percent")
    assert threshold is None


def test_switch_threshold_missing_field() -> None:
    threshold = _get_switch_threshold("claude-code", "nonexistent_field")
    assert threshold is None


@override_settings(
    TEATREE_AGENT_HANDOVER=[
        {"runtime": "test", "telemetry": {"switch_away_at_percent": "not-a-number"}},
    ]
)
def test_switch_threshold_non_numeric_value() -> None:
    threshold = _get_switch_threshold("test", "switch_away_at_percent")
    assert threshold is None


@override_settings(
    TEATREE_AGENT_HANDOVER=[
        {"runtime": "test", "telemetry": {"val": 150}},
    ]
)
def test_switch_threshold_clamped_to_100() -> None:
    assert _get_switch_threshold("test", "val") == 100


@override_settings(
    TEATREE_AGENT_HANDOVER=[
        {"runtime": "test", "telemetry": {"val": -10}},
    ]
)
def test_switch_threshold_clamped_to_0() -> None:
    assert _get_switch_threshold("test", "val") == 0


@override_settings(
    TEATREE_AGENT_HANDOVER=[
        {"runtime": "test", "telemetry": {"val": 75.5}},
    ]
)
def test_switch_threshold_float_value() -> None:
    assert _get_switch_threshold("test", "val") == 75


# --- get_claude_telemetry_path ---


def test_telemetry_path_with_session_id(tmp_path: Path) -> None:
    path = get_claude_telemetry_path("abc-123", state_dir=tmp_path)
    assert path == tmp_path / "abc-123.telemetry.json"


def test_telemetry_path_without_session_id(tmp_path: Path) -> None:
    path = get_claude_telemetry_path("", state_dir=tmp_path)
    assert path == tmp_path / "latest-telemetry.json"


def test_telemetry_path_default_session_id(tmp_path: Path) -> None:
    path = get_claude_telemetry_path(state_dir=tmp_path)
    assert path == tmp_path / "latest-telemetry.json"


# --- load_claude_telemetry ---


def test_load_telemetry_valid_file(tmp_path: Path) -> None:
    telemetry_data = {"session_id": "test-123", "five_hour_used_percentage": 42}
    path = tmp_path / "latest-telemetry.json"
    path.write_text(json.dumps(telemetry_data), encoding="utf-8")

    result = load_claude_telemetry(state_dir=tmp_path)
    assert result == telemetry_data


def test_load_telemetry_missing_file(tmp_path: Path) -> None:
    result = load_claude_telemetry(state_dir=tmp_path)
    assert result == {}


def test_load_telemetry_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "latest-telemetry.json"
    path.write_text("not json", encoding="utf-8")

    result = load_claude_telemetry(state_dir=tmp_path)
    assert result == {}


def test_load_telemetry_non_dict_json(tmp_path: Path) -> None:
    path = tmp_path / "latest-telemetry.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    result = load_claude_telemetry(state_dir=tmp_path)
    assert result == {}


def test_load_telemetry_with_session_id(tmp_path: Path) -> None:
    data = {"session_id": "s1", "five_hour_used_percentage": 10}
    path = tmp_path / "s1.telemetry.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    result = load_claude_telemetry("s1", state_dir=tmp_path)
    assert result["session_id"] == "s1"


# --- should_suggest_handover ---


def test_should_suggest_handover_above_threshold() -> None:
    telemetry = {"five_hour_used_percentage": 96}
    assert should_suggest_handover(telemetry, runtime="claude-code") is True


def test_should_suggest_handover_below_threshold() -> None:
    telemetry = {"five_hour_used_percentage": 50}
    assert should_suggest_handover(telemetry, runtime="claude-code") is False


def test_should_suggest_handover_at_threshold() -> None:
    telemetry = {"five_hour_used_percentage": 95}
    assert should_suggest_handover(telemetry, runtime="claude-code") is True


def test_should_suggest_handover_no_telemetry() -> None:
    assert should_suggest_handover(None, runtime="claude-code") is False


def test_should_suggest_handover_empty_telemetry() -> None:
    assert should_suggest_handover({}, runtime="claude-code") is False


@override_settings(TEATREE_AGENT_HANDOVER=_TWO_RUNTIME_CONFIG)
def test_should_suggest_handover_no_threshold() -> None:
    telemetry = {"five_hour_used_percentage": 96}
    assert should_suggest_handover(telemetry, runtime="codex") is False


def test_should_suggest_handover_non_numeric_used() -> None:
    telemetry = {"five_hour_used_percentage": "not-a-number"}
    assert should_suggest_handover(telemetry, runtime="claude-code") is False


def test_should_suggest_handover_missing_used_key() -> None:
    telemetry = {"some_other_key": 96}
    assert should_suggest_handover(telemetry, runtime="claude-code") is False


# --- get_recommended_runtime ---


@override_settings(TEATREE_AGENT_HANDOVER=_TWO_RUNTIME_CONFIG)
def test_recommended_runtime_triggers_handover() -> None:
    telemetry = {"five_hour_used_percentage": 96}
    assert get_recommended_runtime("claude-code", telemetry) == "codex"


def test_recommended_runtime_no_handover_needed() -> None:
    telemetry = {"five_hour_used_percentage": 50}
    assert get_recommended_runtime("claude-code", telemetry) == ""


@override_settings(TEATREE_AGENT_HANDOVER=_TWO_RUNTIME_CONFIG)
def test_recommended_runtime_recovery_switch_back() -> None:
    """When on fallback runtime and preferred has recovered, switch back."""
    telemetry = {"five_hour_used_percentage": 70}
    assert get_recommended_runtime("codex", telemetry) == "claude-code"


@override_settings(TEATREE_AGENT_HANDOVER=_TWO_RUNTIME_CONFIG)
def test_recommended_runtime_recovery_still_high() -> None:
    """When on fallback but preferred usage still high, stay on fallback."""
    telemetry = {"five_hour_used_percentage": 90}
    assert get_recommended_runtime("codex", telemetry) == ""


def test_recommended_runtime_already_preferred() -> None:
    telemetry = {"five_hour_used_percentage": 50}
    assert get_recommended_runtime("claude-code", telemetry) == ""


@override_settings(TEATREE_AGENT_HANDOVER=_TWO_RUNTIME_CONFIG)
def test_recommended_runtime_no_telemetry() -> None:
    assert get_recommended_runtime("codex", None) == ""


def test_recommended_runtime_no_telemetry_on_preferred() -> None:
    assert get_recommended_runtime("claude-code", None) == ""


@override_settings(
    TEATREE_AGENT_HANDOVER=[
        {"runtime": "a"},
        {"runtime": "b"},
    ]
)
def test_recommended_runtime_no_threshold_for_recovery() -> None:
    """When preferred runtime has no telemetry config, recovery returns empty."""
    telemetry = {"five_hour_used_percentage": 10}
    assert get_recommended_runtime("b", telemetry) == ""


@override_settings(
    TEATREE_AGENT_HANDOVER=[
        {"runtime": "a", "telemetry": {"switch_back_at_percent": 80}},
        {"runtime": "b"},
    ]
)
def test_recommended_runtime_recovery_non_numeric_used() -> None:
    """Non-numeric used percentage prevents recovery switch."""
    telemetry = {"five_hour_used_percentage": "bad"}
    assert get_recommended_runtime("b", telemetry) == ""


# --- build_claude_handover_status ---


def test_build_status_with_telemetry(tmp_path: Path) -> None:
    data = {
        "session_id": "s1",
        "five_hour_used_percentage": 50,
        "five_hour_resets_at": "2025-01-01T00:00:00",
        "context_window_used_percentage": 30,
    }
    path = tmp_path / "latest-telemetry.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    status = build_claude_handover_status(state_dir=tmp_path)

    assert status["session_id"] == "s1"
    assert status["telemetry_available"] is True
    assert status["current_runtime"] == "claude-code"
    assert status["preferred_runtime"] == "claude-code"
    assert status["should_handover"] is False
    assert status["five_hour_used_percentage"] == 50
    assert status["context_window_used_percentage"] == 30
    assert status["five_hour_resets_at"] == "2025-01-01T00:00:00"


def test_build_status_without_telemetry(tmp_path: Path) -> None:
    status = build_claude_handover_status(state_dir=tmp_path)

    assert status["telemetry_available"] is False
    assert status["current_runtime"] == "claude-code"
    assert status["should_handover"] is False
    assert status["session_id"] == ""


@override_settings(TEATREE_AGENT_HANDOVER=_TWO_RUNTIME_CONFIG)
def test_build_status_with_current_runtime(tmp_path: Path) -> None:
    status = build_claude_handover_status(current_runtime="codex", state_dir=tmp_path)
    assert status["current_runtime"] == "codex"


def test_build_status_with_session_id(tmp_path: Path) -> None:
    data = {"session_id": "explicit-session", "five_hour_used_percentage": 20}
    path = tmp_path / "explicit-session.telemetry.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    status = build_claude_handover_status(session_id="explicit-session", state_dir=tmp_path)
    assert status["session_id"] == "explicit-session"


@override_settings(TEATREE_AGENT_HANDOVER=_TWO_RUNTIME_CONFIG)
def test_build_status_handover_triggered(tmp_path: Path) -> None:
    data = {"session_id": "s1", "five_hour_used_percentage": 96}
    path = tmp_path / "latest-telemetry.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    status = build_claude_handover_status(state_dir=tmp_path)
    assert status["should_handover"] is True
    assert status["recommended_runtime"] == "codex"


def test_build_status_includes_agent_handover_config(tmp_path: Path) -> None:
    status = build_claude_handover_status(state_dir=tmp_path)
    assert isinstance(status["agent_handover"], list)
    assert len(status["agent_handover"]) == 1
