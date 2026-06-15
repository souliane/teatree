"""Locate and copy the JSONL Claude Code wrote for a dispatched sub-agent."""

import json
import os
from pathlib import Path

import pytest

from teatree.eval.subagent_capture import capture_to, discover_subagent_files, newest_subagent_transcript


def _subagent_jsonl() -> str:
    return (
        json.dumps(
            {
                "isSidechain": True,
                "agentId": "agent-x",
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
                    "stop_reason": None,
                },
            }
        )
        + "\n"
    )


def _write_subagent(projects: Path, slug: str, session: str, agent_id: str, *, mtime: float | None = None) -> Path:
    path = projects / slug / session / "subagents" / f"agent-{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_subagent_jsonl(), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestDiscoverSubagentFiles:
    def test_finds_across_project_slugs(self, tmp_path: Path) -> None:
        _write_subagent(tmp_path, "slug-a", "sess1", "aaa")
        _write_subagent(tmp_path, "slug-b", "sess2", "bbb")
        found = discover_subagent_files(projects_dir=tmp_path)
        assert {f.path.name for f in found} == {"agent-aaa.jsonl", "agent-bbb.jsonl"}

    def test_since_filters_older_files(self, tmp_path: Path) -> None:
        _write_subagent(tmp_path, "slug", "sess", "old", mtime=1000.0)
        _write_subagent(tmp_path, "slug", "sess", "new", mtime=5000.0)
        found = discover_subagent_files(since=3000.0, projects_dir=tmp_path)
        assert [f.path.name for f in found] == ["agent-new.jsonl"]

    def test_missing_projects_dir_is_empty(self, tmp_path: Path) -> None:
        assert discover_subagent_files(projects_dir=tmp_path / "absent") == []


class TestNewestSubagentTranscript:
    def test_returns_freshest_valid_transcript(self, tmp_path: Path) -> None:
        _write_subagent(tmp_path, "slug", "sess", "old", mtime=1000.0)
        newest = _write_subagent(tmp_path, "slug", "sess", "new", mtime=9000.0)
        assert newest_subagent_transcript(projects_dir=tmp_path) == newest

    def test_skips_non_subagent_shaped_file(self, tmp_path: Path) -> None:
        bogus = tmp_path / "slug" / "sess" / "subagents" / "agent-bogus.jsonl"
        bogus.parent.mkdir(parents=True)
        bogus.write_text(json.dumps({"type": "result", "subtype": "success"}) + "\n", encoding="utf-8")
        assert newest_subagent_transcript(projects_dir=tmp_path) is None


class TestDefaultProjectsDir:
    def test_defaults_to_home_dot_claude_projects(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
        _write_subagent(home / ".claude" / "projects", "slug", "sess", "aaa")
        found = discover_subagent_files()
        assert {f.path.name for f in found} == {"agent-aaa.jsonl"}


class TestCaptureTo:
    def test_copies_to_target(self, tmp_path: Path) -> None:
        source = _write_subagent(tmp_path, "slug", "sess", "aaa")
        target = tmp_path / "out" / "worktree_first.jsonl"
        result = capture_to(target, projects_dir=tmp_path)
        assert result == source
        assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    def test_returns_none_when_nothing_to_capture(self, tmp_path: Path) -> None:
        target = tmp_path / "out" / "worktree_first.jsonl"
        assert capture_to(target, projects_dir=tmp_path) is None
        assert not target.exists()
