"""``teatree.core.harness_todos`` — best-effort read of the harness TODO store.

Backs the recovery snapshots (PreCompact + the continuous stop-snapshotter),
which cannot call the live ``TaskList`` tool; NOT the interactive ``/t3:todos``
view, which would lag the live session. There is no teatree-written
``<session>.todos`` mirror to fall back to — that materialiser was removed.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core import harness_todos


def test_reads_from_harness_task_store() -> None:
    with tempfile.TemporaryDirectory() as tasks_dir:
        session_dir = Path(tasks_dir) / "claude-abc"
        session_dir.mkdir()
        (session_dir / "1.json").write_text(
            json.dumps({"id": "1", "subject": "draft the helper", "status": "pending"}),
            encoding="utf-8",
        )
        (session_dir / "2.json").write_text(
            json.dumps({"id": "2", "subject": "wire the CLI", "status": "in_progress"}),
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"CLAUDE_TASKS_DIR": tasks_dir}):
            todos = harness_todos.read_harness_todos("claude-abc")
    assert todos == [("pending", "draft the helper"), ("in_progress", "wire the CLI")]


def test_orders_by_numeric_task_id() -> None:
    with tempfile.TemporaryDirectory() as tasks_dir:
        session_dir = Path(tasks_dir) / "claude-abc"
        session_dir.mkdir()
        for task_id in ("2", "10", "1"):
            (session_dir / f"{task_id}.json").write_text(
                json.dumps({"id": task_id, "subject": f"task {task_id}", "status": "pending"}),
                encoding="utf-8",
            )
        with patch.dict("os.environ", {"CLAUDE_TASKS_DIR": tasks_dir}):
            todos = harness_todos.read_harness_todos("claude-abc")
    assert [text for _status, text in todos] == ["task 1", "task 2", "task 10"]


def test_skips_malformed_and_subjectless_entries() -> None:
    with tempfile.TemporaryDirectory() as tasks_dir:
        session_dir = Path(tasks_dir) / "claude-abc"
        session_dir.mkdir()
        (session_dir / "1.json").write_text("not json", encoding="utf-8")
        (session_dir / "2.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        (session_dir / "3.json").write_text(json.dumps({"subject": "  "}), encoding="utf-8")
        (session_dir / "4.json").write_text(json.dumps({"subject": "real", "status": ""}), encoding="utf-8")
        with patch.dict("os.environ", {"CLAUDE_TASKS_DIR": tasks_dir}):
            todos = harness_todos.read_harness_todos("claude-abc")
    assert todos == [("pending", "real")]  # blank status defaults to pending


def test_missing_store_is_empty() -> None:
    with (
        tempfile.TemporaryDirectory() as tasks_dir,
        patch.dict("os.environ", {"CLAUDE_TASKS_DIR": tasks_dir}),
    ):
        assert harness_todos.read_harness_todos("claude-abc") == []


def test_empty_session_id_is_empty() -> None:
    assert harness_todos.read_harness_todos("") == []


def test_glob_oserror_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_self: Path, _pattern: str) -> list[Path]:
        raise OSError

    monkeypatch.setattr(Path, "glob", _raise)
    with patch.dict("os.environ", {"CLAUDE_TASKS_DIR": "/whatever"}):
        assert harness_todos.read_harness_todos("s") == []


def test_defaults_to_home_when_env_unset() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CLAUDE_TASKS_DIR", None)
        assert harness_todos._harness_tasks_dir() == Path.home() / ".claude" / "tasks"
