"""``_check_dream_transcript_visibility`` — the `t3 doctor` dream-blindness alarm.

In the Dockerized factory the dream pass globs ``~/.claude/projects`` for session
transcripts. When that dir is unmounted/empty the pass finds 0 members and is a
permanent no-op — invisible until this check. Keys on STRUCTURAL absence (no
``*/*.jsonl`` and no subagent transcript at any age), NOT the 48h recency window,
so a quiet box never false-alarms.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor.checks import _check_dream_transcript_visibility


def _patch_root(root: Path):
    return patch("teatree.loops.dream.engine.default_projects_dir", return_value=root)


class TestDreamTranscriptVisibility:
    def test_missing_projects_dir_warns(self, tmp_path: Path) -> None:
        with _patch_root(tmp_path / "absent"):
            assert _check_dream_transcript_visibility() is False

    def test_empty_projects_dir_warns(self, tmp_path: Path) -> None:
        (tmp_path / "proj").mkdir()
        with _patch_root(tmp_path):
            assert _check_dream_transcript_visibility() is False

    def test_main_transcript_present_ok(self, tmp_path: Path) -> None:
        (tmp_path / "proj").mkdir()
        (tmp_path / "proj" / "sess.jsonl").write_text("{}\n", encoding="utf-8")
        with _patch_root(tmp_path):
            assert _check_dream_transcript_visibility() is True

    def test_only_subagent_transcript_present_ok(self, tmp_path: Path) -> None:
        sub = tmp_path / "proj" / "session" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text("{}\n", encoding="utf-8")
        with _patch_root(tmp_path):
            assert _check_dream_transcript_visibility() is True

    def test_warn_message_names_the_mount(self, tmp_path: Path) -> None:
        buf = io.StringIO()
        with _patch_root(tmp_path / "absent"), redirect_stdout(buf):
            _check_dream_transcript_visibility()
        out = buf.getvalue()
        assert "WARN" in out
        assert "bind mount" in out
        assert ".claude/projects" in out

    def test_crash_degrades_to_ok_with_warn(self) -> None:
        # A crashed advisory read degrades to OK (True) per the docstring — it
        # WARNs but never reddens the run (#3313).
        buf = io.StringIO()
        with (
            patch(
                "teatree.loops.dream.engine.default_projects_dir",
                side_effect=RuntimeError("boom"),
            ),
            redirect_stdout(buf),
        ):
            assert _check_dream_transcript_visibility() is True
        assert "RuntimeError" in buf.getvalue()
