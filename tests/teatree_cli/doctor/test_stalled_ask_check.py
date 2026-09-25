"""``_check_stalled_ask`` — the `t3 doctor check` stalled-ask finding (#4818).

Warns when a transcript's trailing, still-blocking ``AskUserQuestion`` has sat
untouched (file mtime) past ``stalled_ask_minutes``. Reads real transcript files
under a faked ``default_projects_dir()`` — the age gate runs on the file's own
mtime, so a test backdates it directly rather than sleeping.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.cli.doctor.checks_session import _check_stalled_ask
from teatree.core.models import ConfigSetting

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_PROJECTS_DIR = "teatree.loops.dream.replay.default_projects_dir"


def _ask_block(question: str) -> dict:
    tool_use = {"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": [{"question": question}]}}
    return {"type": "assistant", "message": {"role": "assistant", "content": [tool_use]}}


def _write_session_transcript(root: Path, session_id: str, entries: list[dict], *, age_minutes: float = 0) -> Path:
    project_dir = root / "fake-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    if age_minutes:
        stamp = time.time() - age_minutes * 60
        os.utime(path, (stamp, stamp))
    return path


class TestStalledAskCheck:
    def test_no_projects_dir_is_ok_silent(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        with patch(_PROJECTS_DIR, return_value=tmp_path / "does-not-exist"):
            assert _check_stalled_ask() is True
        assert capsys.readouterr().out == ""

    def test_a_fresh_pending_ask_below_threshold_is_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_session_transcript(tmp_path, "s-fresh", [_ask_block("Approve?")], age_minutes=1)
        with patch(_PROJECTS_DIR, return_value=tmp_path):
            assert _check_stalled_ask() is True
        assert capsys.readouterr().out == ""

    def test_an_old_answered_ask_is_silent(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Control: age alone is not the trigger — the ask must still be pending."""
        result = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "1"}]}}
        _write_session_transcript(tmp_path, "s-answered", [_ask_block("Approve?"), result], age_minutes=60)
        with patch(_PROJECTS_DIR, return_value=tmp_path):
            assert _check_stalled_ask() is True
        assert capsys.readouterr().out == ""

    def test_an_old_pending_ask_past_threshold_warns(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_session_transcript(tmp_path, "s-stalled", [_ask_block("Approve the deploy?")], age_minutes=60)
        with patch(_PROJECTS_DIR, return_value=tmp_path):
            assert _check_stalled_ask() is False
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "1 session(s)" in out
        assert "s-stalled" in out

    def test_a_subagent_transcript_is_scanned_too(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        sub_dir = tmp_path / "fake-project" / "s-main" / "subagents"
        sub_dir.mkdir(parents=True)
        path = sub_dir / "agent-worker1.jsonl"
        path.write_text(json.dumps(_ask_block("Approve the sub-task?")) + "\n", encoding="utf-8")
        stamp = time.time() - 60 * 60
        os.utime(path, (stamp, stamp))
        with patch(_PROJECTS_DIR, return_value=tmp_path):
            assert _check_stalled_ask() is False
        assert "agent-worker" in capsys.readouterr().out  # session-name preview truncates to 12 chars

    def test_the_configured_threshold_is_honoured(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A 5-minute override fires on a 10-minute-old ask the 20-minute default would miss."""
        ConfigSetting.objects.set_value("stalled_ask_minutes", 5)
        _write_session_transcript(tmp_path, "s-mid", [_ask_block("Approve?")], age_minutes=10)
        with patch(_PROJECTS_DIR, return_value=tmp_path):
            assert _check_stalled_ask() is False
        assert "s-mid" in capsys.readouterr().out

    def test_threshold_zero_disables_the_check(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        ConfigSetting.objects.set_value("stalled_ask_minutes", 0)
        _write_session_transcript(tmp_path, "s-stalled", [_ask_block("Approve?")], age_minutes=999)
        with patch(_PROJECTS_DIR, return_value=tmp_path):
            assert _check_stalled_ask() is True
        assert capsys.readouterr().out == ""

    def test_a_crash_degrades_to_ok(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch(_PROJECTS_DIR, side_effect=RuntimeError("boom")):
            assert _check_stalled_ask() is True
        assert "WARN" in capsys.readouterr().out
