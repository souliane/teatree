"""Tests for teetree.core.views.history — _extract_text, _load_transcript, SessionHistoryView."""

import json

import pytest
from django.test import Client
from django.urls import reverse

from teetree.core.views.history import _extract_text, _load_transcript

# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


def test_extract_text_from_string() -> None:
    assert _extract_text("hello world") == "hello world"


def test_extract_text_from_list_with_text_blocks() -> None:
    content = [
        {"type": "text", "text": "First paragraph."},
        {"type": "text", "text": "Second paragraph."},
    ]
    assert _extract_text(content) == "First paragraph.\nSecond paragraph."


def test_extract_text_from_list_with_tool_use_block() -> None:
    content = [
        {"type": "tool_use", "name": "Read"},
    ]
    assert _extract_text(content) == "[tool: Read]"


def test_extract_text_from_list_with_mixed_blocks() -> None:
    content = [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "name": "Bash"},
        {"type": "text", "text": "Done."},
    ]
    assert _extract_text(content) == "Let me check.\n[tool: Bash]\nDone."


def test_extract_text_from_list_ignores_non_dict_items() -> None:
    content = ["just a string", {"type": "text", "text": "ok"}, 42]
    assert _extract_text(content) == "ok"


def test_extract_text_from_list_handles_unknown_type() -> None:
    content = [{"type": "image", "url": "http://example.com/img.png"}]
    assert _extract_text(content) == ""


def test_extract_text_returns_empty_for_unsupported_type() -> None:
    assert _extract_text(12345) == ""
    assert _extract_text(None) == ""
    assert _extract_text({}) == ""


def test_extract_text_empty_list() -> None:
    assert _extract_text([]) == ""


# ---------------------------------------------------------------------------
# _load_transcript
# ---------------------------------------------------------------------------


def test_load_transcript_returns_empty_for_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.setattr("teetree.core.views.history._CLAUDE_PROJECTS_DIR", tmp_path)
    result = _load_transcript("nonexistent-session", "/some/path")
    assert result == []


def test_load_transcript_parses_user_and_assistant_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.setattr("teetree.core.views.history._CLAUDE_PROJECTS_DIR", tmp_path)

    project_dir = tmp_path / "-some-path"
    project_dir.mkdir(parents=True)
    jsonl_path = project_dir / "session-123.jsonl"

    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "Hello"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "Hi there"}}),
    ]
    jsonl_path.write_text("\n".join(lines), encoding="utf-8")

    result = _load_transcript("session-123", "/some/path")

    assert len(result) == 2
    assert result[0] == {"role": "user", "text": "Hello"}
    assert result[1] == {"role": "assistant", "text": "Hi there"}


def test_load_transcript_skips_non_user_assistant_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.setattr("teetree.core.views.history._CLAUDE_PROJECTS_DIR", tmp_path)

    project_dir = tmp_path / "-some-path"
    project_dir.mkdir(parents=True)
    jsonl_path = project_dir / "sess.jsonl"

    lines = [
        json.dumps({"type": "system", "message": {"role": "system", "content": "System prompt"}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "Question"}}),
        json.dumps({"type": "result", "message": {"content": "Done"}}),
    ]
    jsonl_path.write_text("\n".join(lines), encoding="utf-8")

    result = _load_transcript("sess", "/some/path")

    assert len(result) == 1
    assert result[0]["role"] == "user"


def test_load_transcript_skips_invalid_json_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.setattr("teetree.core.views.history._CLAUDE_PROJECTS_DIR", tmp_path)

    project_dir = tmp_path / "-some-path"
    project_dir.mkdir(parents=True)
    jsonl_path = project_dir / "sess.jsonl"

    lines = [
        "not valid json",
        json.dumps({"type": "user", "message": {"role": "user", "content": "Works"}}),
    ]
    jsonl_path.write_text("\n".join(lines), encoding="utf-8")

    result = _load_transcript("sess", "/some/path")

    assert len(result) == 1
    assert result[0]["text"] == "Works"


def test_load_transcript_skips_empty_text_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.setattr("teetree.core.views.history._CLAUDE_PROJECTS_DIR", tmp_path)

    project_dir = tmp_path / "-some-path"
    project_dir.mkdir(parents=True)
    jsonl_path = project_dir / "sess.jsonl"

    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": ""}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "   "}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "Real content"}}),
    ]
    jsonl_path.write_text("\n".join(lines), encoding="utf-8")

    result = _load_transcript("sess", "/some/path")

    assert len(result) == 1
    assert result[0]["text"] == "Real content"


def test_load_transcript_truncates_long_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.setattr("teetree.core.views.history._CLAUDE_PROJECTS_DIR", tmp_path)

    project_dir = tmp_path / "-some-path"
    project_dir.mkdir(parents=True)
    jsonl_path = project_dir / "sess.jsonl"

    long_text = "x" * 10000
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": long_text}})]
    jsonl_path.write_text("\n".join(lines), encoding="utf-8")

    result = _load_transcript("sess", "/some/path")

    assert len(result) == 1
    assert len(result[0]["text"]) == 5000


def test_load_transcript_with_list_content(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
    monkeypatch.setattr("teetree.core.views.history._CLAUDE_PROJECTS_DIR", tmp_path)

    project_dir = tmp_path / "-some-path"
    project_dir.mkdir(parents=True)
    jsonl_path = project_dir / "sess.jsonl"

    content = [{"type": "text", "text": "Structured response"}]
    lines = [json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}})]
    jsonl_path.write_text("\n".join(lines), encoding="utf-8")

    result = _load_transcript("sess", "/some/path")

    assert len(result) == 1
    assert result[0]["text"] == "Structured response"


# ---------------------------------------------------------------------------
# SessionHistoryView
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.django_db


def test_session_history_returns_200_with_valid_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.setattr("teetree.core.views.history._CLAUDE_PROJECTS_DIR", tmp_path)

    project_dir = tmp_path / "-workspace-project"
    project_dir.mkdir(parents=True)
    jsonl_path = project_dir / "abc123.jsonl"

    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "Hello"}})]
    jsonl_path.write_text("\n".join(lines), encoding="utf-8")

    response = Client().get(
        reverse("teetree:session-history", args=["abc123"]),
        {"cwd": "/workspace/project"},
    )

    assert response.status_code == 200


def test_session_history_returns_404_without_cwd() -> None:
    response = Client().get(reverse("teetree:session-history", args=["abc123"]))

    assert response.status_code == 404


def test_session_history_returns_404_with_invalid_cwd() -> None:
    response = Client().get(
        reverse("teetree:session-history", args=["abc123"]),
        {"cwd": "/path with spaces/bad!chars"},
    )

    assert response.status_code == 404


def test_session_history_returns_404_when_transcript_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.setattr("teetree.core.views.history._CLAUDE_PROJECTS_DIR", tmp_path)

    response = Client().get(
        reverse("teetree:session-history", args=["nonexistent"]),
        {"cwd": "/workspace/project"},
    )

    assert response.status_code == 404
