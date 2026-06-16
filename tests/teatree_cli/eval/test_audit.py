"""``t3 eval audit`` — the conversation-audit CLI over recent on-disk sessions.

Lean integration: real session jsonl files written under a temp ``$HOME``'s
``~/.claude/projects/<cwd-slug>/`` directory, listed through the production
``claude_sessions`` reader, audited by the #1861 engine, and persisted to the
real test DB. No model call anywhere.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.core.models import SessionAuditRecord


def _cwd_key() -> str:
    return str(Path.cwd()).replace("/", "-").lstrip("-")


def _write_session(home: Path, session_id: str, body: str) -> Path:
    project_dir = home / ".claude" / "projects" / _cwd_key()
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    path.write_text(body, encoding="utf-8")
    return path


def _assistant_tool(name: str, tool_input: dict[str, object]) -> str:
    content = [{"type": "tool_use", "id": "t1", "name": name, "input": tool_input}]
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}}) + "\n"


_VIOLATING = _assistant_tool("Bash", {"command": "git push --force origin main"})
_CLEAN = _assistant_tool("Bash", {"command": "ls"})


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestEvalAudit:
    def test_audits_recent_sessions_and_persists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_session(tmp_path, "sess-clean", _CLEAN)
        _write_session(tmp_path, "sess-force", _VIOLATING)
        result = CliRunner().invoke(app, ["eval", "audit", "--limit", "10"])
        assert result.exit_code == 0, result.output
        assert "sess-clean" in result.output
        assert "sess-force" in result.output
        assert SessionAuditRecord.objects.count() == 2
        assert SessionAuditRecord.objects.for_session("sess-force").get().nominated_for_label is True
        assert "nominated for label: 1" in result.output

    def test_session_flag_audits_only_that_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_session(tmp_path, "sess-clean", _CLEAN)
        _write_session(tmp_path, "sess-force", _VIOLATING)
        result = CliRunner().invoke(app, ["eval", "audit", "--session", "sess-clean"])
        assert result.exit_code == 0, result.output
        assert SessionAuditRecord.objects.count() == 1
        assert SessionAuditRecord.objects.for_session("sess-clean").exists()

    def test_missing_session_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        result = CliRunner().invoke(app, ["eval", "audit", "--session", "no-such-session"])
        assert result.exit_code == 2
        assert "no session jsonl found" in result.output
        assert SessionAuditRecord.objects.count() == 0

    def test_no_sessions_in_scope_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        result = CliRunner().invoke(app, ["eval", "audit"])
        assert result.exit_code == 0, result.output
        assert "no sessions in scope" in result.output
        assert SessionAuditRecord.objects.count() == 0

    def test_vanished_session_file_is_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_session(tmp_path, "sess-clean", _CLEAN)
        with patch("teatree.cli.eval.audit.find_session_file", return_value=None):
            result = CliRunner().invoke(app, ["eval", "audit"])
        assert result.exit_code == 0, result.output
        assert "no sessions in scope" in result.output

    def test_confusion_renders_matrix_text(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_session(tmp_path, "sess-force", _VIOLATING)
        result = CliRunner().invoke(app, ["eval", "audit", "--confusion", "conformance"])
        assert result.exit_code == 0, result.output
        assert "axis=conformance" in result.output
        assert "accuracy:" in result.output

    def test_re_auditing_a_session_does_not_inflate_the_matrix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_session(tmp_path, "sess-force", _VIOLATING)
        CliRunner().invoke(app, ["eval", "audit", "--session", "sess-force"])
        result = CliRunner().invoke(
            app, ["eval", "audit", "--session", "sess-force", "--confusion", "conformance", "--json"]
        )
        assert result.exit_code == 0, result.output
        assert SessionAuditRecord.objects.count() == 2
        payload = json.loads(result.output[result.output.index("{") :])
        assert payload["total"] == 1

    def test_confusion_json_renders_machine_readable_matrix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        SessionAuditRecord.record(
            session_id="seeded",
            corpus_entry_id="",
            outcome_axis="conformance",
            expected_outcome="clean",
            predicted_outcome="one_shot",
            verdict="skip",
            oracle="invariant",
        )
        result = CliRunner().invoke(app, ["eval", "audit", "--confusion", "conformance", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output[result.output.index("{") :])
        assert payload["axis"] == "conformance"
        assert payload["total"] == 1

    def test_json_without_confusion_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        result = CliRunner().invoke(app, ["eval", "audit", "--json"])
        assert result.exit_code == 2
        assert "--json requires --confusion" in result.output

    def test_corpus_label_matched_by_source_session_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        body = _assistant_tool(
            "AskUserQuestion",
            {"questions": [{"question": "Which deployment target should this build go to?"}]},
        )
        _write_session(tmp_path, "synthetic-aq-001", body)
        result = CliRunner().invoke(app, ["eval", "audit", "--session", "synthetic-aq-001"])
        assert result.exit_code == 0, result.output
        record = SessionAuditRecord.objects.for_session("synthetic-aq-001").get()
        assert record.corpus_entry_id == "structured_question"
        assert record.verdict == "pass"

    def test_table_never_prints_payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_session(tmp_path, "sess-force", _VIOLATING)
        result = CliRunner().invoke(app, ["eval", "audit"])
        assert result.exit_code == 0, result.output
        assert "git push --force" not in result.output
