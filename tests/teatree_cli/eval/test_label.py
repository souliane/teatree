"""``t3 eval label nominate/add/review`` — corpus-label curation over the audit ledger.

Lean integration: real ``SessionAuditRecord`` rows in the test DB, real session
jsonl under a temp ``$HOME``, real label yaml validated through
``corpus_loader.discover_corpus``. The redaction guard is the production
publication scanner, never a stub.
"""

import json
from pathlib import Path

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


def _assistant_bash(command: str) -> str:
    content = [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}]
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}}) + "\n"


def _record(session_id: str, *, nominated: bool = True) -> SessionAuditRecord:
    return SessionAuditRecord.record(
        session_id=session_id,
        corpus_entry_id="",
        outcome_axis="conformance",
        expected_outcome="clean",
        predicted_outcome="one_shot",
        verdict="skip",
        oracle="invariant",
        gate_failure_slugs=["inline-question"],
        nominated_for_label=nominated,
    )


def _label_yaml(entry_id: str, *, labelled_by: str = "human:rev", rule_author: str = "skills/code") -> str:
    return (
        f"- entry_id: {entry_id}\n"
        f"  labelled_by: {labelled_by}\n"
        '  labelled_at: "2026-06-10"\n'
        "  expected_behavior: worktree before any edit\n"
        "  outcome_axis: axis_q\n"
        "  expected_outcome: ok_done\n"
        "  confidence: high\n"
        "  oracle: matcher\n"
        f"  rule_author: {rule_author}\n"
        "  expect:\n"
        "    - tool_call: Bash\n"
        '      args.command: contains "git worktree add"\n'
    )


@pytest.mark.django_db
class TestLabelNominate:
    def test_lists_nominated_records_with_slugs(self) -> None:
        _record("sess-nom", nominated=True)
        _record("sess-quiet", nominated=False)
        result = CliRunner().invoke(app, ["eval", "label", "nominate"])
        assert result.exit_code == 0, result.output
        assert "sess-nom" in result.output
        assert "sess-quiet" not in result.output
        assert "conformance" in result.output
        assert "one_shot" in result.output
        assert "inline-question" in result.output

    def test_empty_nomination_queue_prints_placeholder(self) -> None:
        result = CliRunner().invoke(app, ["eval", "label", "nominate"])
        assert result.exit_code == 0, result.output
        assert "(no records nominated for labelling)" in result.output


@pytest.mark.django_db
class TestLabelAdd:
    def test_scaffolds_session_copy_and_label_template(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        corpus_dir = tmp_path / "corpus"
        _record("sess-add")
        _write_session(tmp_path, "sess-add", _assistant_bash("ls"))
        result = CliRunner().invoke(
            app, ["eval", "label", "add", "sess-add", "--dir", str(corpus_dir), "--entry-id", "my_entry"]
        )
        assert result.exit_code == 0, result.output
        assert (corpus_dir / "my_entry.session.jsonl").is_file()
        label_text = (corpus_dir / "my_entry.label.yaml").read_text(encoding="utf-8")
        assert "entry_id: my_entry" in label_text
        assert '"conformance"' in label_text
        assert '"clean"' in label_text
        assert 'labelled_by: ""' in label_text
        assert '"sess-add"' in label_text
        assert str(corpus_dir / "my_entry.label.yaml") in result.output

    def test_default_entry_id_is_sanitized_from_session_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        corpus_dir = tmp_path / "corpus"
        _record("Sess-Add.01")
        _write_session(tmp_path, "Sess-Add.01", _assistant_bash("ls"))
        result = CliRunner().invoke(app, ["eval", "label", "add", "Sess-Add.01", "--dir", str(corpus_dir)])
        assert result.exit_code == 0, result.output
        assert (corpus_dir / "sess_add_01.label.yaml").is_file()

    def test_redaction_hit_refuses_and_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        corpus_dir = tmp_path / "corpus"
        _record("sess-leak")
        _write_session(tmp_path, "sess-leak", _assistant_bash("echo the user said verbatim do it"))
        result = CliRunner().invoke(app, ["eval", "label", "add", "sess-leak", "--dir", str(corpus_dir)])
        assert result.exit_code == 1, result.output
        assert "REFUSED" in result.output
        assert not corpus_dir.exists() or not list(corpus_dir.glob("*"))

    def test_no_audit_record_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_session(tmp_path, "sess-x", _assistant_bash("ls"))
        result = CliRunner().invoke(app, ["eval", "label", "add", "sess-x", "--dir", str(tmp_path / "corpus")])
        assert result.exit_code == 2
        assert "no audit record" in result.output

    def test_missing_session_file_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _record("sess-gone")
        result = CliRunner().invoke(app, ["eval", "label", "add", "sess-gone", "--dir", str(tmp_path / "corpus")])
        assert result.exit_code == 2
        assert "no session jsonl found" in result.output

    def test_existing_entry_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "my_entry.label.yaml").write_text("existing", encoding="utf-8")
        _record("sess-dup")
        _write_session(tmp_path, "sess-dup", _assistant_bash("ls"))
        result = CliRunner().invoke(
            app, ["eval", "label", "add", "sess-dup", "--dir", str(corpus_dir), "--entry-id", "my_entry"]
        )
        assert result.exit_code == 2
        assert "already exists" in result.output
        assert (corpus_dir / "my_entry.label.yaml").read_text(encoding="utf-8") == "existing"


class TestLabelReview:
    def test_valid_corpus_passes(self, tmp_path: Path) -> None:
        (tmp_path / "good.label.yaml").write_text(_label_yaml("good"), encoding="utf-8")
        (tmp_path / "good.session.jsonl").write_text(_assistant_bash("git worktree add ../wt"), encoding="utf-8")
        result = CliRunner().invoke(app, ["eval", "label", "review", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_circular_matcher_oracle_exits_nonzero(self, tmp_path: Path) -> None:
        (tmp_path / "circ.label.yaml").write_text(
            _label_yaml("circ", labelled_by="human:author", rule_author="human:author"), encoding="utf-8"
        )
        (tmp_path / "circ.session.jsonl").write_text(_assistant_bash("ls"), encoding="utf-8")
        result = CliRunner().invoke(app, ["eval", "label", "review", "--dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "circular" in result.output

    def test_unloadable_label_exits_nonzero(self, tmp_path: Path) -> None:
        (tmp_path / "broken.label.yaml").write_text(_label_yaml("broken"), encoding="utf-8")
        result = CliRunner().invoke(app, ["eval", "label", "review", "--dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "FAILED" in result.output

    def test_shipped_corpus_reviews_green(self) -> None:
        result = CliRunner().invoke(app, ["eval", "label", "review"])
        assert result.exit_code == 0, result.output
        assert "OK" in result.output
