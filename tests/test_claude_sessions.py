"""Tests for ``teetree.claude_sessions`` — session listing and status detection."""

import json
import time
from pathlib import Path

import pytest

from teetree.claude_sessions import (
    _build_session_index,
    _extract_first_user_message,
    _session_end_status,
    list_sessions,
)

# ── _session_end_status ──────────────────────────────────────────────


class TestSessionEndStatus:
    def test_finished_when_last_entry_is_last_prompt(self, tmp_path: Path) -> None:
        conv = tmp_path / "abc123.jsonl"
        conv.write_text(
            _jsonl(
                {"type": "user", "message": {"role": "user", "content": "hello"}},
                {"type": "assistant", "message": {"stop_reason": "end_turn"}},
                {"type": "system", "subtype": "turn_duration"},
                {"type": "last-prompt", "lastPrompt": "hello", "sessionId": "abc123"},
            )
        )
        assert _session_end_status(conv) == "finished"

    def test_interrupted_when_last_entry_is_user(self, tmp_path: Path) -> None:
        conv = tmp_path / "abc123.jsonl"
        conv.write_text(
            _jsonl(
                {"type": "user", "message": {"role": "user", "content": "hello"}},
                {"type": "assistant", "message": {"stop_reason": "end_turn"}},
                {"type": "user", "message": {"role": "user", "content": "another question"}},
            )
        )
        assert _session_end_status(conv) == "interrupted"

    def test_interrupted_when_last_entry_is_progress(self, tmp_path: Path) -> None:
        conv = tmp_path / "abc123.jsonl"
        conv.write_text(
            _jsonl(
                {"type": "user", "message": {"role": "user", "content": "hello"}},
                {"type": "progress", "data": "thinking..."},
            )
        )
        assert _session_end_status(conv) == "interrupted"

    def test_interrupted_when_last_entry_is_assistant_without_last_prompt(self, tmp_path: Path) -> None:
        conv = tmp_path / "abc123.jsonl"
        conv.write_text(
            _jsonl(
                {"type": "user", "message": {"role": "user", "content": "hello"}},
                {"type": "assistant", "message": {"stop_reason": "end_turn"}},
            )
        )
        assert _session_end_status(conv) == "interrupted"

    def test_unknown_when_file_missing(self, tmp_path: Path) -> None:
        conv = tmp_path / "nonexistent.jsonl"
        assert _session_end_status(conv) == "unknown"

    def test_unknown_when_last_line_is_invalid_json(self, tmp_path: Path) -> None:
        conv = tmp_path / "abc123.jsonl"
        conv.write_text('{"type": "user"}\nnot valid json\n')
        assert _session_end_status(conv) == "unknown"

    def test_handles_trailing_newlines(self, tmp_path: Path) -> None:
        conv = tmp_path / "abc123.jsonl"
        conv.write_text(
            _jsonl(
                {"type": "last-prompt", "lastPrompt": "hi", "sessionId": "abc123"},
            )
            + "\n\n"
        )
        assert _session_end_status(conv) == "finished"


# ── _build_session_index ─────────────────────────────────────────────


class TestBuildSessionIndex:
    def test_reads_history_entries(self, tmp_path: Path) -> None:
        history = tmp_path / "history.jsonl"
        history.write_text(
            _jsonl(
                {
                    "sessionId": "s1",
                    "display": "first prompt",
                    "timestamp": 1700000000000,
                    "project": "/home/user/proj",
                },
                {
                    "sessionId": "s1",
                    "display": "second prompt",
                    "timestamp": 1700000001000,
                    "project": "/home/user/proj",
                },
                {"sessionId": "s2", "display": "other session", "timestamp": 1700000002000, "project": "/tmp/other"},
            )
        )
        index = _build_session_index(history)
        assert len(index) == 2
        assert index["s1"]["first_prompt"] == "first prompt"
        assert index["s1"]["timestamp"] == 1700000000000
        assert index["s2"]["project"] == "/tmp/other"

    def test_first_entry_wins_per_session(self, tmp_path: Path) -> None:
        history = tmp_path / "history.jsonl"
        history.write_text(
            _jsonl(
                {"sessionId": "s1", "display": "original", "timestamp": 100, "project": "/a"},
                {"sessionId": "s1", "display": "later", "timestamp": 200, "project": "/b"},
            )
        )
        index = _build_session_index(history)
        assert index["s1"]["first_prompt"] == "original"
        assert index["s1"]["timestamp"] == 100

    def test_empty_file(self, tmp_path: Path) -> None:
        history = tmp_path / "history.jsonl"
        history.write_text("")
        assert _build_session_index(history) == {}

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _build_session_index(tmp_path / "nope.jsonl") == {}


# ── _extract_first_user_message ──────────────────────────────────────


class TestExtractFirstUserMessage:
    def test_extracts_string_content(self, tmp_path: Path) -> None:
        conv = tmp_path / "s.jsonl"
        conv.write_text(
            _jsonl(
                {"type": "file-history-snapshot", "snapshot": {}},
                {"type": "user", "message": {"role": "user", "content": "what is this?"}, "timestamp": 42},
            )
        )
        prompt, ts = _extract_first_user_message(conv)
        assert prompt == "what is this?"
        assert ts == 42

    def test_extracts_list_content(self, tmp_path: Path) -> None:
        conv = tmp_path / "s.jsonl"
        conv.write_text(
            _jsonl(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "hello from list"}],
                    },
                    "timestamp": 99,
                },
            )
        )
        prompt, ts = _extract_first_user_message(conv)
        assert prompt == "hello from list"
        assert ts == 99

    def test_returns_empty_for_no_user_message(self, tmp_path: Path) -> None:
        conv = tmp_path / "s.jsonl"
        conv.write_text(_jsonl({"type": "file-history-snapshot", "snapshot": {}}))
        prompt, ts = _extract_first_user_message(conv)
        assert prompt == ""
        assert ts == 0

    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        prompt, ts = _extract_first_user_message(tmp_path / "nope.jsonl")
        assert prompt == ""
        assert ts == 0


# ── list_sessions ────────────────────────────────────────────────────


class TestListSessions:
    def _make_project(
        self,
        projects_dir: Path,
        project_name: str,
        sessions: list[tuple[str, list[dict]]],
    ) -> None:
        """Create a project dir with conversation JSONL files."""
        d = projects_dir / project_name
        d.mkdir(parents=True)
        for sid, entries in sessions:
            conv = d / f"{sid}.jsonl"
            conv.write_text(_jsonl(*entries))
            # Space out mtimes so ordering is deterministic
            time.sleep(0.01)

    def test_lists_sessions_from_project_dir(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        history = tmp_path / "history.jsonl"
        history.write_text("")

        self._make_project(
            projects_dir,
            "-test-project",
            [
                (
                    "s1",
                    [
                        {"type": "user", "message": {"role": "user", "content": "hello"}, "timestamp": 100},
                        {"type": "last-prompt", "lastPrompt": "hello", "sessionId": "s1"},
                    ],
                ),
            ],
        )

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            cwd="/test/project",
            all_projects=True,
        )
        assert len(results) == 1
        assert results[0].session_id == "s1"
        assert results[0].status == "finished"

    def test_filters_by_cwd(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        history = tmp_path / "history.jsonl"
        history.write_text("")

        for name in ("-Users-me-workspace", "-Users-me-other"):
            self._make_project(
                projects_dir,
                name,
                [("s-" + name, [{"type": "last-prompt", "lastPrompt": "", "sessionId": "s-" + name}])],
            )

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            cwd="/Users/me/workspace",
        )
        assert len(results) == 1
        assert results[0].session_id == "s--Users-me-workspace"

    def test_all_projects_flag(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        history = tmp_path / "history.jsonl"
        history.write_text("")

        for name in ("-proj-a", "-proj-b"):
            self._make_project(
                projects_dir,
                name,
                [("s-" + name, [{"type": "last-prompt", "lastPrompt": "", "sessionId": "s-" + name}])],
            )

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            cwd="/unrelated",
            all_projects=True,
        )
        assert len(results) == 2

    def test_project_filter(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        history = tmp_path / "history.jsonl"
        history.write_text("")

        for name in ("-my-frontend", "-my-backend"):
            self._make_project(
                projects_dir,
                name,
                [("s-" + name, [{"type": "last-prompt", "lastPrompt": "", "sessionId": "s-" + name}])],
            )

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            cwd="/unrelated",
            project_filter="backend",
        )
        assert len(results) == 1
        assert results[0].session_id == "s--my-backend"

    def test_limit(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        history = tmp_path / "history.jsonl"
        history.write_text("")

        entries = [(f"s{i}", [{"type": "last-prompt", "lastPrompt": "", "sessionId": f"s{i}"}]) for i in range(5)]
        self._make_project(projects_dir, "-proj", entries)

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            all_projects=True,
            limit=3,
        )
        assert len(results) == 3

    def test_uses_history_index_for_first_prompt(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        history = tmp_path / "history.jsonl"
        history.write_text(
            _jsonl(
                {"sessionId": "s1", "display": "from history", "timestamp": 5000, "project": "/my/proj"},
            )
        )

        self._make_project(
            projects_dir,
            "-my-proj",
            [("s1", [{"type": "last-prompt", "lastPrompt": "", "sessionId": "s1"}])],
        )

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            all_projects=True,
        )
        assert results[0].first_prompt == "from history"
        assert results[0].cwd == "/my/proj"

    def test_empty_projects_dir(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        history = tmp_path / "history.jsonl"
        history.write_text("")

        results = list_sessions(projects_dir=projects_dir, history_file=history, all_projects=True)
        assert results == []

    def test_missing_projects_dir(self, tmp_path: Path) -> None:
        results = list_sessions(
            projects_dir=tmp_path / "nonexistent",
            history_file=tmp_path / "history.jsonl",
            all_projects=True,
        )
        assert results == []

    def test_skips_subagent_dirs(self, tmp_path: Path) -> None:
        """Subagent directories contain JSONL files but shouldn't be listed."""
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-proj"
        proj.mkdir(parents=True)
        (proj / "main.jsonl").write_text(_jsonl({"type": "last-prompt", "lastPrompt": "", "sessionId": "main"}))
        # subagents/ is a nested dir — glob("*.jsonl") on the project dir won't match it
        sub = proj / "main" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-xyz.jsonl").write_text(
            _jsonl({"type": "last-prompt", "lastPrompt": "", "sessionId": "agent-xyz"})
        )

        history = tmp_path / "history.jsonl"
        history.write_text("")

        results = list_sessions(projects_dir=projects_dir, history_file=history, all_projects=True)
        assert len(results) == 1
        assert results[0].session_id == "main"


# ── Helpers ──────────────────────────────────────────────────────────


def _jsonl(*entries: dict) -> str:
    return "\n".join(json.dumps(e) for e in entries) + "\n"


# ── _is_session_running ──────────────────────────────────────────────

from teetree.claude_sessions import _is_session_running, _parse_user_content, _safe_json  # noqa: E402


class TestIsSessionRunning:
    def test_returns_false_when_no_sessions_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Point Path.home() to a dir without .claude/sessions
        monkeypatch.setattr("teetree.claude_sessions.Path.home", lambda: tmp_path)
        assert _is_session_running("any-id") is False

    def test_returns_true_for_matching_alive_process(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import os  # noqa: PLC0415

        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr("teetree.claude_sessions.Path.home", lambda: tmp_path)

        current_pid = os.getpid()
        session_data = {"pid": current_pid, "sessionId": "target-session"}
        (sessions_dir / "session.json").write_text(json.dumps(session_data), encoding="utf-8")

        assert _is_session_running("target-session") is True

    def test_returns_false_for_dead_process(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr("teetree.claude_sessions.Path.home", lambda: tmp_path)

        session_data = {"pid": 999_999_999, "sessionId": "dead-session"}
        (sessions_dir / "session.json").write_text(json.dumps(session_data), encoding="utf-8")

        assert _is_session_running("dead-session") is False

    def test_returns_false_for_non_matching_session_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import os  # noqa: PLC0415

        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr("teetree.claude_sessions.Path.home", lambda: tmp_path)

        current_pid = os.getpid()
        session_data = {"pid": current_pid, "sessionId": "other-session"}
        (sessions_dir / "session.json").write_text(json.dumps(session_data), encoding="utf-8")

        assert _is_session_running("wanted-session") is False

    def test_skips_invalid_json_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr("teetree.claude_sessions.Path.home", lambda: tmp_path)

        (sessions_dir / "bad.json").write_text("not json", encoding="utf-8")

        assert _is_session_running("any-id") is False

    def test_returns_false_for_missing_pid_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr("teetree.claude_sessions.Path.home", lambda: tmp_path)

        session_data = {"sessionId": "target"}  # No "pid" key
        (sessions_dir / "session.json").write_text(json.dumps(session_data), encoding="utf-8")

        assert _is_session_running("target") is False


# ── _session_end_status with active session ──────────────────────────


class TestSessionEndStatusActive:
    def test_active_when_session_is_running(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import os  # noqa: PLC0415

        # Set up the sessions dir
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr("teetree.claude_sessions.Path.home", lambda: tmp_path)

        current_pid = os.getpid()
        session_data = {"pid": current_pid, "sessionId": "active-conv"}
        (sessions_dir / "session.json").write_text(json.dumps(session_data), encoding="utf-8")

        # Create conv file whose last entry is NOT last-prompt
        conv = tmp_path / "active-conv.jsonl"
        conv.write_text(
            _jsonl(
                {"type": "user", "message": {"role": "user", "content": "hello"}},
                {"type": "assistant", "message": {"stop_reason": "end_turn"}},
            )
        )

        assert _session_end_status(conv) == "active"


# ── _build_session_index edge cases ──────────────────────────────────


class TestBuildSessionIndexEdgeCases:
    def test_skips_invalid_json_lines(self, tmp_path: Path) -> None:
        history = tmp_path / "history.jsonl"
        history.write_text(
            "not valid json\n"
            + json.dumps({"sessionId": "s1", "display": "good", "timestamp": 100, "project": "/a"})
            + "\n"
        )
        index = _build_session_index(history)
        assert len(index) == 1
        assert index["s1"]["first_prompt"] == "good"

    def test_skips_entries_without_session_id(self, tmp_path: Path) -> None:
        history = tmp_path / "history.jsonl"
        history.write_text(json.dumps({"display": "no sid", "timestamp": 100, "project": "/a"}) + "\n")
        index = _build_session_index(history)
        assert index == {}


# ── _parse_user_content edge cases ───────────────────────────────────


class TestParseUserContent:
    def test_returns_empty_for_non_dict_message(self) -> None:
        assert _parse_user_content({"message": "not-a-dict"}) == ""

    def test_returns_empty_for_list_content_without_text_type(self) -> None:
        entry = {"message": {"content": [{"type": "image", "url": "img.png"}]}}
        assert _parse_user_content(entry) == ""

    def test_returns_empty_for_list_content_with_non_dict_parts(self) -> None:
        entry = {"message": {"content": ["not-a-dict"]}}
        assert _parse_user_content(entry) == ""


# ── _safe_json edge cases ────────────────────────────────────────────


class TestSafeJson:
    def test_returns_none_for_non_dict_json(self) -> None:
        assert _safe_json('"just a string"') is None

    def test_returns_none_for_list_json(self) -> None:
        assert _safe_json("[1, 2, 3]") is None

    def test_returns_none_for_invalid_json(self) -> None:
        assert _safe_json("not json") is None


# ── _build_session_info / list_sessions edge cases ───────────────────


class TestBuildSessionInfoEdgeCases:
    def test_session_not_in_index_uses_conv_file(self, tmp_path: Path) -> None:
        """When session is not in history index, first prompt and ts come from the conv file."""
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-proj"
        proj.mkdir(parents=True)
        history = tmp_path / "history.jsonl"
        history.write_text("")

        conv = proj / "unknown-session.jsonl"
        conv.write_text(
            _jsonl(
                {"type": "user", "message": {"role": "user", "content": "from conv"}, "timestamp": 0},
                {"type": "last-prompt", "lastPrompt": "", "sessionId": "unknown-session"},
            )
        )

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            all_projects=True,
        )

        assert len(results) == 1
        assert results[0].first_prompt == "from conv"
        # timestamp=0 in conv → falls back to mtime-based timestamp
        assert results[0].timestamp > 0

    def test_session_with_cwd_replaces_home(self, tmp_path: Path) -> None:
        """When session has cwd, it should have home replaced with ~."""
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-proj"
        proj.mkdir(parents=True)
        history = tmp_path / "history.jsonl"

        home = str(Path.home())
        history.write_text(
            _jsonl(
                {
                    "sessionId": "s1",
                    "display": "hello",
                    "timestamp": 5000,
                    "project": f"{home}/workspace/proj",
                },
            )
        )

        conv = proj / "s1.jsonl"
        conv.write_text(_jsonl({"type": "last-prompt", "lastPrompt": "", "sessionId": "s1"}))

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            all_projects=True,
        )

        assert len(results) == 1
        assert results[0].project.startswith("~")

    def test_session_without_cwd_uses_dir_name(self, tmp_path: Path) -> None:
        """When session has no cwd in history, project label falls back to dir name."""
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-my-project"
        proj.mkdir(parents=True)
        history = tmp_path / "history.jsonl"

        # Entry has empty project string
        history.write_text(
            _jsonl(
                {"sessionId": "s1", "display": "hello", "timestamp": 5000, "project": ""},
            )
        )

        conv = proj / "s1.jsonl"
        conv.write_text(_jsonl({"type": "last-prompt", "lastPrompt": "", "sessionId": "s1"}))

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            all_projects=True,
        )

        assert len(results) == 1
        assert results[0].project == "-my-project"

    def test_skips_non_dir_entries_in_projects_dir(self, tmp_path: Path) -> None:
        """Non-directory entries in projects_dir should be skipped (line 168)."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        history = tmp_path / "history.jsonl"
        history.write_text("")

        # Create a regular file in the projects dir (not a directory)
        (projects_dir / "not-a-dir.txt").write_text("hello")

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            all_projects=True,
        )

        assert results == []

    def test_session_timestamp_fallback_to_mtime(self, tmp_path: Path) -> None:
        """When session is not in index and conv has ts=0, timestamp falls back to mtime."""
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-proj"
        proj.mkdir(parents=True)
        history = tmp_path / "history.jsonl"
        history.write_text("")

        conv = proj / "no-index.jsonl"
        # User message with timestamp 0, so it triggers the fallback `ts or int(mtime * 1000)`
        conv.write_text(
            _jsonl(
                {"type": "user", "message": {"role": "user", "content": "hello"}, "timestamp": 0},
                {"type": "last-prompt", "lastPrompt": "", "sessionId": "no-index"},
            )
        )

        results = list_sessions(
            projects_dir=projects_dir,
            history_file=history,
            all_projects=True,
        )

        assert len(results) == 1
        # timestamp should be mtime-based (non-zero), not 0
        assert results[0].timestamp > 0


# ── _parse_user_content: content is neither str nor list ─────────────


class TestParseUserContentNonStrNonList:
    def test_returns_empty_for_int_content(self) -> None:
        """When content is neither str nor list (e.g. int), return empty."""
        entry = {"message": {"content": 42}}
        assert _parse_user_content(entry) == ""


# ── list_sessions with explicit SessionQuery ─────────────────────────

from teetree.claude_sessions import SessionQuery  # noqa: E402


class TestListSessionsWithQuery:
    def test_with_explicit_query_object(self, tmp_path: Path) -> None:
        """Passing a SessionQuery object directly covers the `query is not None` branch."""
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-proj"
        proj.mkdir(parents=True)
        history = tmp_path / "history.jsonl"
        history.write_text("")

        conv = proj / "s1.jsonl"
        conv.write_text(_jsonl({"type": "last-prompt", "lastPrompt": "", "sessionId": "s1"}))

        query = SessionQuery(
            projects_dir=projects_dir,
            history_file=history,
            all_projects=True,
        )
        results = list_sessions(query)

        assert len(results) == 1
        assert results[0].session_id == "s1"
