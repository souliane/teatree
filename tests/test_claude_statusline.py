import json
import os
import re
import subprocess
from pathlib import Path


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

    result = subprocess.run(
        ["./integrations/claude-code-statusline/statusline-command.sh"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=Path(__file__).resolve().parent.parent,
    )

    assert result.returncode == 0, result.stderr
    output = _strip_ansi(result.stdout)
    assert "5h=96%" in output

    latest = json.loads((state_dir / "latest-telemetry.json").read_text(encoding="utf-8"))
    assert latest["session_id"] == "session-123"
    assert latest["five_hour_used_percentage"] == 96
    assert latest["five_hour_resets_at"] == "2026-03-23T17:00:00Z"

    session_file = state_dir / "session-123.telemetry.json"
    assert session_file.is_file()
