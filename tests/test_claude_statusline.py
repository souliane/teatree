import json
import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)


def test_statusline_renders_and_persists_five_hour_usage(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "teatree"
    cwd.mkdir(parents=True)
    state_dir = tmp_path / "state"

    payload = {
        "workspace": {"current_dir": str(cwd)},
        "model": {"display_name": "Claude Sonnet"},
        "session_id": "session-123",
        "context_window": {"used_percentage": 41.8},
        "rate_limits": {
            "five_hour": {
                "used_percentage": 96.4,
                "resets_at": "2026-03-23T17:00:00Z",
            }
        },
    }

    env = os.environ.copy()
    env["T3_WORKSPACE_DIR"] = str(workspace)
    env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
    env["TZ"] = "UTC"

    result = subprocess.run(
        ["./hooks/scripts/statusline-command.sh"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=Path(__file__).resolve().parent.parent,
    )

    assert result.returncode == 0, result.stderr
    output = _strip_ansi(result.stdout)
    assert "96%" in output
    assert "17:00" in output, f"Reset time not displayed in output: {output}"

    latest = json.loads((state_dir / "latest-telemetry.json").read_text(encoding="utf-8"))
    assert latest["session_id"] == "session-123"
    assert latest["five_hour_used_percentage"] == 96
    assert latest["five_hour_resets_at"] == "2026-03-23T17:00:00Z"

    session_file = state_dir / "session-123.telemetry.json"
    assert session_file.is_file()


def test_statusline_displays_loaded_skills(tmp_path: Path) -> None:
    """Skills tracked by the PostToolUse hook appear in the statusline output."""
    workspace = tmp_path / "workspace"
    cwd = workspace / "teatree"
    cwd.mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Pre-populate the skills file (normally written by hook_router.py)
    skills_file = state_dir / "session-skills.skills"
    skills_file.write_text("t3:code\nt3:debug\n", encoding="utf-8")

    payload = {
        "workspace": {"current_dir": str(cwd)},
        "model": {"display_name": "Claude Opus"},
        "session_id": "session-skills",
        "context_window": {"used_percentage": 20},
        "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": ""}},
    }

    env = os.environ.copy()
    env["T3_WORKSPACE_DIR"] = str(workspace)
    env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)

    result = subprocess.run(
        ["./hooks/scripts/statusline-command.sh"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=Path(__file__).resolve().parent.parent,
    )

    assert result.returncode == 0, result.stderr
    output = _strip_ansi(result.stdout)
    assert "skills:" in output, f"Skills section missing from statusline: {output}"
    assert "t3:code" in output, f"t3:code not in statusline: {output}"
    assert "t3:debug" in output, f"t3:debug not in statusline: {output}"


def test_statusline_omits_skills_section_when_no_skills(tmp_path: Path) -> None:
    """When no skills are loaded, the skills section is omitted entirely."""
    workspace = tmp_path / "workspace"
    cwd = workspace / "teatree"
    cwd.mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    payload = {
        "workspace": {"current_dir": str(cwd)},
        "model": {"display_name": "Claude Opus"},
        "session_id": "session-noskills",
        "context_window": {"used_percentage": 20},
        "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": ""}},
    }

    env = os.environ.copy()
    env["T3_WORKSPACE_DIR"] = str(workspace)
    env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)

    result = subprocess.run(
        ["./hooks/scripts/statusline-command.sh"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=Path(__file__).resolve().parent.parent,
    )

    assert result.returncode == 0, result.stderr
    output = _strip_ansi(result.stdout)
    assert "skills:" not in output, f"Skills section should not appear: {output}"


def test_statusline_handles_epoch_resets_at(tmp_path: Path) -> None:
    """resets_at may be a Unix epoch (seconds) instead of ISO 8601."""
    workspace = tmp_path / "workspace"
    cwd = workspace / "teatree"
    cwd.mkdir(parents=True)
    state_dir = tmp_path / "state"

    # 1775062800 = 2026-04-01T17:00:00 UTC
    payload = {
        "workspace": {"current_dir": str(cwd)},
        "model": {"display_name": "Claude Opus"},
        "session_id": "session-epoch",
        "context_window": {"used_percentage": 10},
        "rate_limits": {
            "five_hour": {
                "used_percentage": 30,
                "resets_at": "1775062800",
            }
        },
    }

    env = os.environ.copy()
    env["T3_WORKSPACE_DIR"] = str(workspace)
    env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
    env["TZ"] = "UTC"

    result = subprocess.run(
        ["./hooks/scripts/statusline-command.sh"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=Path(__file__).resolve().parent.parent,
    )

    assert result.returncode == 0, result.stderr
    output = _strip_ansi(result.stdout)
    assert "30%" in output
    assert "17:00" in output, f"Reset time not displayed for epoch input: {output}"
