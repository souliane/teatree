"""`resolve_transcript` picks the right session JSONL for transcript-replay."""

from pathlib import Path
from unittest.mock import patch

from teatree.claude_sessions import SessionInfo
from teatree.cli.eval.transcript_replay import replay_transcript_for_all, resolve_transcript

_MODULE = "teatree.cli.eval.transcript_replay"


def _session(session_id: str) -> SessionInfo:
    return SessionInfo(
        session_id=session_id,
        project="proj",
        first_prompt="hi",
        timestamp=0,
        mtime=0.0,
        cwd="/tmp",
        status="finished",
    )


class TestResolveTranscript:
    def test_explicit_file_that_exists_wins(self, tmp_path: Path) -> None:
        f = tmp_path / "t.jsonl"
        f.write_text("{}", encoding="utf-8")
        assert resolve_transcript(latest=True, session=None, file=f) == f

    def test_explicit_file_that_is_missing_resolves_to_none(self, tmp_path: Path) -> None:
        assert resolve_transcript(latest=True, session=None, file=tmp_path / "absent.jsonl") is None

    def test_no_latest_and_no_session_resolves_to_none(self) -> None:
        with patch(f"{_MODULE}.list_sessions", return_value=[_session("s1")]):
            assert resolve_transcript(latest=False, session=None, file=None) is None

    def test_session_match_resolves_to_its_jsonl(self, tmp_path: Path) -> None:
        project = tmp_path / ".claude" / "projects" / "proj"
        project.mkdir(parents=True)
        target = project / "s1.jsonl"
        target.write_text("{}", encoding="utf-8")
        with (
            patch(f"{_MODULE}.list_sessions", return_value=[_session("s1")]),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            assert resolve_transcript(latest=False, session="s1", file=None) == target

    def test_latest_picks_first_session(self, tmp_path: Path) -> None:
        project = tmp_path / ".claude" / "projects" / "proj"
        project.mkdir(parents=True)
        target = project / "newest.jsonl"
        target.write_text("{}", encoding="utf-8")
        with (
            patch(f"{_MODULE}.list_sessions", return_value=[_session("newest"), _session("older")]),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            assert resolve_transcript(latest=True, session=None, file=None) == target

    def test_latest_with_no_sessions_resolves_to_none(self) -> None:
        with patch(f"{_MODULE}.list_sessions", return_value=[]):
            assert resolve_transcript(latest=True, session=None, file=None) is None

    def test_session_match_but_no_jsonl_on_disk_resolves_to_none(self, tmp_path: Path) -> None:
        (tmp_path / ".claude" / "projects" / "proj").mkdir(parents=True)
        with (
            patch(f"{_MODULE}.list_sessions", return_value=[_session("s1")]),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            assert resolve_transcript(latest=False, session="s1", file=None) is None

    def test_missing_projects_dir_resolves_to_none(self, tmp_path: Path) -> None:
        with (
            patch(f"{_MODULE}.list_sessions", return_value=[_session("s1")]),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            assert resolve_transcript(latest=False, session="s1", file=None) is None


class TestReplayTranscriptForAll:
    def test_returns_none_when_no_transcript_in_scope(self) -> None:
        with patch(f"{_MODULE}.resolve_transcript", return_value=None):
            assert replay_transcript_for_all() is None

    def test_replays_present_transcript(self, tmp_path: Path) -> None:
        fixture = Path(__file__).parents[1] / "fixtures" / "transcripts" / "all_pass.session.jsonl"
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
        with patch(f"{_MODULE}.resolve_transcript", return_value=transcript):
            results = replay_transcript_for_all()
        assert results is not None
        assert all(r.ok for r in results)
