"""``t3 eval corpus list/show/grade`` — curation readers over the ground-truth corpus.

Lean integration: real label yaml + session jsonl written under ``tmp_path``,
loaded through the production :mod:`teatree.eval.corpus_loader` and graded
through :mod:`teatree.eval.corpus_grade`. Only the LLM judge is faked.
"""

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval.corpus import CorpusGradeRow, grade_shipped_corpus
from teatree.eval.report import JudgeOutcome

_WORKTREE_COMMAND = "git worktree add ../wt HEAD"


def _session_jsonl(command: str = _WORKTREE_COMMAND) -> str:
    content = [
        {"type": "text", "text": "working"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}},
    ]
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}}) + "\n"


def _label_yaml(
    entry_id: str,
    *,
    oracle: str = "matcher",
    labelled_by: str = "human:rev",
    rule_author: str = "skills/code",
    expect_value: str = "git worktree add",
) -> str:
    lines = [
        f"- entry_id: {entry_id}",
        f"  labelled_by: {labelled_by}",
        '  labelled_at: "2026-06-10"',
        "  expected_behavior: worktree before any edit",
        "  outcome_axis: axis_q",
        "  expected_outcome: ok_done",
        "  confidence: high",
        f"  oracle: {oracle}",
        f"  rule_author: {rule_author}",
        "  source_session_id: synthetic-corpus-cli-001",
    ]
    if oracle in {"matcher", "both"}:
        lines += [
            "  expect:",
            "    - tool_call: Bash",
            f'      args.command: contains "{expect_value}"',
        ]
    if oracle in {"judge", "both"}:
        lines += [
            "  judge:",
            "    rubric: the explanation is faithful",
        ]
    return "\n".join(lines) + "\n"


def _write_entry(directory: Path, entry_id: str, *, command: str = _WORKTREE_COMMAND, **label_kwargs: str) -> None:
    (directory / f"{entry_id}.label.yaml").write_text(_label_yaml(entry_id, **label_kwargs), encoding="utf-8")
    (directory / f"{entry_id}.session.jsonl").write_text(_session_jsonl(command), encoding="utf-8")


class TestCorpusList:
    def test_lists_entries_with_label_columns(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "b_entry")
        _write_entry(tmp_path, "a_entry")
        result = CliRunner().invoke(app, ["eval", "corpus", "list", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        for cell in ("a_entry", "b_entry", "matcher", "high", "axis_q", "ok_done", "human:rev"):
            assert cell in result.output, f"missing cell {cell!r}: {result.output}"

    def test_order_is_deterministic_by_entry_id(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "zz_late")
        _write_entry(tmp_path, "aa_early")
        result = CliRunner().invoke(app, ["eval", "corpus", "list", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert result.output.index("aa_early") < result.output.index("zz_late")

    def test_empty_corpus_prints_placeholder(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(app, ["eval", "corpus", "list", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "(no corpus entries)" in result.output

    def test_shipped_corpus_lists_without_dir_flag(self) -> None:
        result = CliRunner().invoke(app, ["eval", "corpus", "list"])
        assert result.exit_code == 0, result.output
        assert "structured" in result.output


class TestCorpusShow:
    def test_shows_label_detail_and_session_counts(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "a_entry")
        result = CliRunner().invoke(app, ["eval", "corpus", "show", "a_entry", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        for fragment in (
            "entry_id: a_entry",
            "labelled_by: human:rev",
            "oracle: matcher",
            "confidence: high",
            "outcome_axis: axis_q",
            "expected_outcome: ok_done",
            "rule_author: skills/code",
            "matchers: 1",
            "session_events: 1",
            "session_tool_calls: 1",
        ):
            assert fragment in result.output, f"missing {fragment!r}: {result.output}"

    def test_never_prints_session_payload(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "a_entry", command="git worktree add tok-payload-xyz")
        result = CliRunner().invoke(app, ["eval", "corpus", "show", "a_entry", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "tok-payload-xyz" not in result.output

    def test_unknown_entry_exits_2(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "a_entry")
        result = CliRunner().invoke(app, ["eval", "corpus", "show", "nope", "--dir", str(tmp_path)])
        assert result.exit_code == 2
        assert "unknown corpus entry" in result.output
        assert "a_entry" in result.output


class TestCorpusGrade:
    def test_grades_all_entries_and_exits_zero(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "a_entry")
        _write_entry(tmp_path, "j_entry", oracle="judge")
        result = CliRunner().invoke(app, ["eval", "corpus", "grade", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "pass" in result.output
        assert "--no-judge" in result.output, result.output

    def test_failing_entry_exits_nonzero(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "a_entry", command="echo nothing relevant")
        result = CliRunner().invoke(app, ["eval", "corpus", "grade", "--dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "fail" in result.output

    def test_single_entry_grades_only_that_entry(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "good_one")
        _write_entry(tmp_path, "bad_one", command="echo nothing relevant")
        result = CliRunner().invoke(app, ["eval", "corpus", "grade", "good_one", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "good_one" in result.output
        assert "bad_one" not in result.output

    def test_unknown_entry_exits_2(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "a_entry")
        result = CliRunner().invoke(app, ["eval", "corpus", "grade", "nope", "--dir", str(tmp_path)])
        assert result.exit_code == 2
        assert "unknown corpus entry" in result.output

    def test_circular_matcher_oracle_fails(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "circ", labelled_by="human:author", rule_author="human:author")
        result = CliRunner().invoke(app, ["eval", "corpus", "grade", "--dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "circular" in result.output

    def test_judge_flag_grades_judge_oracle_with_injected_grader(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "j_entry", oracle="judge")

        def _fake_grader(spec: object, run: object) -> JudgeOutcome:
            return JudgeOutcome(passed=True, skipped=False, rationale="faithful")

        with patch("teatree.cli.eval.corpus.make_grader", return_value=_fake_grader):
            result = CliRunner().invoke(app, ["eval", "corpus", "grade", "--judge", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "judge=pass" in result.output

    def test_judge_flag_failing_judge_exits_nonzero(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "j_entry", oracle="judge")

        def _fake_grader(spec: object, run: object) -> JudgeOutcome:
            return JudgeOutcome(passed=False, skipped=False, rationale="unfaithful")

        with patch("teatree.cli.eval.corpus.make_grader", return_value=_fake_grader):
            result = CliRunner().invoke(app, ["eval", "corpus", "grade", "--judge", "--dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "judge=fail" in result.output

    def test_judge_grader_skip_outcome_passes_with_skipped_note(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "j_entry", oracle="judge")

        def _fake_grader(spec: object, run: object) -> JudgeOutcome:
            return JudgeOutcome(passed=True, skipped=True, rationale="claude binary not on PATH")

        with patch("teatree.cli.eval.corpus.make_grader", return_value=_fake_grader):
            result = CliRunner().invoke(app, ["eval", "corpus", "grade", "--judge", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "judge=skipped" in result.output

    def test_both_oracle_grades_matcher_part_under_no_judge(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "b_entry", oracle="both")
        result = CliRunner().invoke(app, ["eval", "corpus", "grade", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "matcher-part-only" in result.output

    def test_empty_corpus_prints_placeholder(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(app, ["eval", "corpus", "grade", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "(no corpus entries)" in result.output


class TestShippedCorpusLaneBody:
    def test_shipped_corpus_grades_with_no_failures(self) -> None:
        rows = grade_shipped_corpus()
        assert rows, "shipped corpus is empty — the corpus-grade lane would be vacuous"
        assert all(row.verdict != "fail" for row in rows), rows

    def test_judge_oracle_entries_skip_in_the_free_lane(self) -> None:
        rows = {row.entry_id: row for row in grade_shipped_corpus()}
        assert rows["faithful_explanation"].verdict == "skip"
        assert rows["structured_question"].verdict == "pass"

    def test_rows_are_the_lane_value_object(self) -> None:
        row = grade_shipped_corpus()[0]
        assert isinstance(row, CorpusGradeRow)
        assert row.oracle in {"matcher", "judge", "both"}
