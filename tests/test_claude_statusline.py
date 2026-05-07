"""Tests for ``hooks/scripts/statusline.sh`` — the Claude Code statusline hook.

The hook composes two info streams: the loop's pre-rendered zones file (anchors,
action_needed, in_flight) and live per-session info from Claude's stdin JSON
(model, ctx %, loaded skills).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hooks" / "scripts" / "statusline.sh"


def _run(payload: dict, *, state_dir: Path, statusline_file: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
    if statusline_file is not None:
        env["TEATREE_STATUSLINE_FILE"] = str(statusline_file)
    return subprocess.run(
        [str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=REPO_ROOT,
    )


class TestStatuslineHook:
    def test_displays_loaded_skills_from_session_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "session-skills.skills").write_text("t3:code\nt3:debug\n", encoding="utf-8")

        result = _run(
            {"session_id": "session-skills", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "skills: t3:code t3:debug" in result.stdout

    def test_omits_skills_when_session_file_absent(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {"session_id": "no-skills", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "skills:" not in result.stdout

    def test_renders_model_and_context_window_from_stdin(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {
                "session_id": "s1",
                "model": {"display_name": "Claude Sonnet"},
                "context_window": {"used_percentage": 41.8},
            },
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "model=Claude Sonnet" in result.stdout
        assert "ctx=41%" in result.stdout

    def test_appends_pre_rendered_loop_zones_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        statusline_file = tmp_path / "statusline.txt"
        statusline_file.write_text("tick @ 2026-05-07T12:00:00\nIn flight:\n→ statusline: x\n", encoding="utf-8")

        result = _run(
            {"session_id": "s1", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=statusline_file,
        )

        assert result.returncode == 0, result.stderr
        assert "model=Claude Opus" in result.stdout
        assert "tick @ 2026-05-07T12:00:00" in result.stdout
        assert "→ statusline: x" in result.stdout

    def test_handles_missing_loop_file_gracefully(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        missing = tmp_path / "nope.txt"

        result = _run(
            {"session_id": "s1", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=missing,
        )

        assert result.returncode == 0, result.stderr
        assert "model=Claude Opus" in result.stdout

    def test_no_session_id_emits_no_skills_section(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # A skills file exists but the payload has no session_id — must not pick it up
        (state_dir / ".skills").write_text("rogue\n", encoding="utf-8")

        result = _run(
            {"model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "skills:" not in result.stdout
        assert "rogue" not in result.stdout
