"""Opt-in redacted transcript tail viewer (#3673 Tier 2).

The viewer resolves an ``agent_session_id`` to its on-disk Claude transcript,
tails a bounded number of lines (never the whole file), and redacts every line
through the shared leak-gate policy before it reaches a template.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.dash import transcript
from teatree.dash.transcript import TranscriptEntry, TranscriptLine, tail_transcript, transcript_path


def _write_transcript(projects_dir: Path, session_id: str, entries: list[dict]) -> Path:
    project = projects_dir / "-home-op-work"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


class TranscriptPathTestCase(TestCase):
    def test_resolves_session_id_to_its_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            written = _write_transcript(projects, "sess-1", [{"type": "user", "message": {"content": "hi"}}])
            found = transcript.transcript_path("sess-1", projects_dir=projects)
            assert found == written

    def test_missing_session_id_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assert transcript.transcript_path("nope", projects_dir=Path(tmp)) is None

    def test_blank_session_id_is_none(self) -> None:
        assert transcript.transcript_path("") is None

    def test_absent_projects_dir_is_none(self) -> None:
        # A projects root that is not a directory on disk resolves to None
        # rather than raising (the tailer degrades to an empty drawer).
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "does-not-exist"
            assert transcript_path("sess", projects_dir=absent) is None


class TailTranscriptTestCase(TestCase):
    def test_tail_is_bounded_and_never_returns_more_than_the_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            entries = [{"type": "user", "message": {"content": f"line {i}"}} for i in range(500)]
            _write_transcript(projects, "big", entries)
            rows = transcript.tail_transcript("big", projects_dir=projects, lines=50)
            assert len(rows) <= 50
            # the tail keeps the MOST RECENT lines
            assert rows[-1].text.endswith("499")

    def test_missing_transcript_returns_empty_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assert transcript.tail_transcript("gone", projects_dir=Path(tmp)) == []

    def test_lines_are_redacted_through_the_leak_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            _write_transcript(
                projects,
                "leaky",
                [{"type": "assistant", "message": {"content": "the codename ACMECORP appears here"}}],
            )
            with patch(
                "teatree.dash.transcript.redact_for_local_display",
                side_effect=lambda t: t.replace("ACMECORP", "[redacted]"),
            ):
                rows = transcript.tail_transcript("leaky", projects_dir=projects)
            assert "ACMECORP" not in rows[0].text
            assert "[redacted]" in rows[0].text

    def test_malformed_line_is_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            project = projects / "-p"
            project.mkdir(parents=True)
            (project / "mixed.jsonl").write_text(
                'not json\n{"type": "user", "message": {"content": "ok"}}\n', encoding="utf-8"
            )
            rows = transcript.tail_transcript("mixed", projects_dir=projects)
            assert any(r.text == "ok" for r in rows)

    def test_unreadable_transcript_fails_open_to_empty(self) -> None:
        # The resolved transcript path is a DIRECTORY (matches the *.jsonl glob),
        # so open() raises OSError — the tailer logs and returns [] rather than
        # crashing the drawer.
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            (projects / "-p" / "dir.jsonl").mkdir(parents=True)
            assert tail_transcript("dir", projects_dir=projects) == []

    def test_blank_and_non_dict_and_empty_lines_are_all_dropped(self) -> None:
        # A blank line, a JSON scalar/array (not an object), and an object whose
        # extracted preview is empty each yield no row; only the real entry survives.
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            project = projects / "-p"
            project.mkdir(parents=True)
            (project / "sparse.jsonl").write_text(
                "\n"  # blank line -> dropped
                "[1, 2, 3]\n"  # a JSON array, not an object -> dropped
                '{"type": "user", "message": {"content": ""}}\n'  # empty preview -> dropped
                '{"type": "user", "message": {"content": 42}}\n'  # non-str/list content -> dropped
                '{"type": "assistant", "message": {"content": "kept"}}\n',
                encoding="utf-8",
            )
            rows = tail_transcript("sparse", projects_dir=projects)
            assert [r.text for r in rows] == ["kept"]

    def test_non_message_entry_falls_back_to_its_own_content(self) -> None:
        # An entry with no nested `message` key reads its top-level `content`.
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            _write_transcript(projects, "summ", [{"type": "summary", "content": "a rollup line"}])
            rows = tail_transcript("summ", projects_dir=projects)
            assert rows == [TranscriptEntry(role="summary", text="a rollup line")]

    def test_content_block_list_collapses_to_a_preview(self) -> None:
        # A content-block list at every branch: a text block (str and non-str),
        # a tool_use (named and unnamed), a tool_result, a non-dict item, and an
        # unknown block kind — the preview keeps only the readable parts.
        blocks: TranscriptLine = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hello world"},
                    {"type": "text", "text": 999},  # non-str text -> dropped
                    {"type": "tool_use", "name": "Bash"},
                    {"type": "tool_use", "name": None},  # non-str name -> dropped
                    {"type": "tool_result", "content": "ignored"},
                    "a bare string, not a block",  # non-dict item -> skipped
                    {"type": "thinking"},  # unknown kind -> ignored
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            _write_transcript(projects, "blocks", [dict(blocks)])
            rows = tail_transcript("blocks", projects_dir=projects)
            assert rows[0].role == "assistant"
            assert rows[0].text == "hello world [tool_use: Bash] [tool_result]"
