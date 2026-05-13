"""Tests for ``hooks/scripts/statusline.sh`` — the Claude Code statusline hook.

The hook composes two info streams: the loop's pre-rendered zones file (anchors,
action_needed, in_flight) and live per-session info from Claude's stdin JSON
(model, ctx %, loaded skills).
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hooks" / "scripts" / "statusline.sh"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;[^\x1b]*\x1b\\")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


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
        # Skill tokens are space-separated now (previously `|`) — the colored
        # magenta names provide enough visual separation on their own.
        assert "skills: t3:code t3:debug" in _strip_ansi(result.stdout)

    def test_omits_skills_when_session_file_absent(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {"session_id": "no-skills", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "skills:" not in _strip_ansi(result.stdout)

    def test_renders_rate_limits_from_stdin(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {
                "session_id": "s1",
                "model": {"display_name": "Claude Opus"},
                "rate_limits": {
                    "five_hour": {"used_percentage": 42, "resets_at": "1747047000"},
                    "seven_day": {"used_percentage": 85},
                },
            },
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "5h=42%" in plain
        assert "7d=85%" in plain

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
        plain = _strip_ansi(result.stdout)
        assert "model=Claude Sonnet" in plain
        assert "ctx=41%" in plain

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
        plain = _strip_ansi(result.stdout)
        assert "model=Claude Opus" in plain
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
        assert "model=Claude Opus" in _strip_ansi(result.stdout)

    def test_renders_cron_jobs_from_state_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        crons = {
            "jobs": {
                "job-1": {"name": "tick", "cron": "*/12 * * * *", "cadence": 720, "created_at": 0},
                "job-2": {"name": "followup", "cron": "*/5 * * * *", "cadence": 300, "created_at": 0},
            },
            "wakeup": None,
        }
        (state_dir / "s-cron.crons").write_text(json.dumps(crons), encoding="utf-8")

        result = _run(
            {"session_id": "s-cron", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "loops:" in plain
        assert "tick(12m)" in plain
        assert "followup(5m)" in plain

    def test_renders_schedule_wakeup_countdown(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        crons = {
            "jobs": {},
            "wakeup": {"name": "checking build", "next_epoch": int(time.time()) + 180},
        }
        (state_dir / "s-wake.crons").write_text(json.dumps(crons), encoding="utf-8")

        result = _run(
            {"session_id": "s-wake", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "loops:" in plain
        assert "checking build" in plain

    def test_omits_loops_when_no_crons_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {"session_id": "s-none", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "loops:" not in _strip_ansi(result.stdout)

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
        plain = _strip_ansi(result.stdout)
        assert "skills:" not in plain
        assert "rogue" not in plain
