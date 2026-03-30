import json
from pathlib import Path

from django.test import override_settings
from typer.testing import CliRunner

from teatree.agents.handover import (
    build_claude_handover_status,
    get_agent_handover_config,
    get_claude_statusline_state_dir,
    get_claude_telemetry_path,
    get_recommended_runtime,
    load_claude_telemetry,
    should_suggest_handover,
)
from teatree.cli import app

runner = CliRunner()


def test_get_agent_handover_config_defaults_to_ordered_cli_agents() -> None:
    assert get_agent_handover_config() == [
        {
            "runtime": "claude-code",
            "telemetry": {
                "provider": "claude-statusline",
                "switch_away_at_percent": 95,
                "switch_back_at_percent": 80,
            },
        },
        {
            "runtime": "codex",
        },
    ]


@override_settings(TEATREE_CLAUDE_STATUSLINE_STATE_DIR="/tmp/claude-statusline-test")
def test_get_claude_statusline_state_dir_reads_setting() -> None:
    assert get_claude_statusline_state_dir() == Path("/tmp/claude-statusline-test")


def test_get_claude_telemetry_path_supports_session_files(tmp_path: Path) -> None:
    assert get_claude_telemetry_path("session-123", state_dir=tmp_path) == tmp_path / "session-123.telemetry.json"


@override_settings(
    TEATREE_AGENT_HANDOVER=[
        {
            "runtime": "claude-code",
            "telemetry": {
                "provider": "claude-statusline",
                "switch_away_at_percent": 90,
                "switch_back_at_percent": 70,
            },
        },
        {"runtime": "codex"},
    ]
)
def test_should_suggest_handover_uses_configured_threshold_from_agent_list() -> None:
    assert should_suggest_handover({"five_hour_used_percentage": 90}, runtime="claude-code") is True
    assert should_suggest_handover({"five_hour_used_percentage": 89}, runtime="claude-code") is False


def test_should_suggest_handover_returns_false_without_telemetry() -> None:
    assert should_suggest_handover({}, runtime="claude-code") is False


def test_should_suggest_handover_rejects_non_numeric_usage() -> None:
    assert should_suggest_handover({"five_hour_used_percentage": "96"}, runtime="claude-code") is False


def test_should_suggest_handover_returns_false_for_runtime_without_telemetry() -> None:
    assert should_suggest_handover({"five_hour_used_percentage": 96}, runtime="codex") is False


def test_load_claude_telemetry_returns_empty_for_bad_json(tmp_path: Path) -> None:
    (tmp_path / "latest-telemetry.json").write_text("{bad json", encoding="utf-8")

    assert load_claude_telemetry(state_dir=tmp_path) == {}


def test_load_claude_telemetry_returns_empty_for_non_mapping_json(tmp_path: Path) -> None:
    (tmp_path / "latest-telemetry.json").write_text('["not", "a", "dict"]', encoding="utf-8")

    assert load_claude_telemetry(state_dir=tmp_path) == {}


def test_build_claude_handover_status_uses_session_specific_file(tmp_path: Path) -> None:
    (tmp_path / "session-123.telemetry.json").write_text(
        json.dumps(
            {
                "session_id": "session-123",
                "five_hour_used_percentage": 97,
                "five_hour_resets_at": "2026-03-23T17:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    status = build_claude_handover_status(session_id="session-123", state_dir=tmp_path)

    assert status["session_id"] == "session-123"
    assert status["telemetry_available"] is True
    assert status["should_handover"] is True


def test_get_recommended_runtime_switches_to_next_runtime_when_current_is_limited() -> None:
    assert (
        get_recommended_runtime(
            current_runtime="claude-code",
            telemetry={"five_hour_used_percentage": 96},
        )
        == "codex"
    )


def test_get_recommended_runtime_switches_back_to_preferred_runtime_when_recovered() -> None:
    assert (
        get_recommended_runtime(
            current_runtime="codex",
            telemetry={"five_hour_used_percentage": 79},
        )
        == "claude-code"
    )


def test_get_recommended_runtime_returns_empty_when_no_switch_is_needed() -> None:
    assert get_recommended_runtime(current_runtime="codex", telemetry={"five_hour_used_percentage": 90}) == ""


@override_settings(TEATREE_CLAUDE_STATUSLINE_STATE_DIR="/tmp/unused-for-test")
def test_tool_claude_handover_reports_latest_status(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "latest-telemetry.json").write_text(
        json.dumps(
            {
                "session_id": "session-123",
                "five_hour_used_percentage": 96,
                "five_hour_resets_at": "2026-03-23T17:00:00Z",
                "context_window_used_percentage": 42,
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "tool",
            "claude-handover",
            "--json",
            "--current-runtime",
            "claude-code",
            "--state-dir",
            str(telemetry_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["should_handover"] is True
    assert payload["current_runtime"] == "claude-code"
    assert payload["recommended_runtime"] == "codex"
    assert payload["five_hour_used_percentage"] == 96


def test_tool_claude_handover_renders_plain_text(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "latest-telemetry.json").write_text(
        json.dumps(
            {
                "session_id": "session-456",
                "five_hour_used_percentage": 94,
                "five_hour_resets_at": "2026-03-23T18:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "tool",
            "claude-handover",
            "--current-runtime",
            "codex",
            "--state-dir",
            str(telemetry_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "5h=94%" in result.stdout
    assert "current=codex" in result.stdout
    assert "recommended=stay" in result.stdout
