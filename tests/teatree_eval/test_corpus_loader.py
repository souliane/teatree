"""Discover and validate ground-truth corpus labels."""

from pathlib import Path

import pytest

from teatree.eval.corpus_loader import discover_corpus, load_corpus_label
from teatree.eval.loader import EvalSpecError
from teatree.eval.models import Matcher

_MATCHER_LABEL = (
    "- entry_id: sample_matcher\n"
    "  labelled_by: human:reviewer\n"
    '  labelled_at: "2026-06-09"\n'
    "  expected_behavior: backgrounds the long operation\n"
    "  outcome_axis: backgrounding\n"
    "  expected_outcome: backgrounded\n"
    "  confidence: high\n"
    "  oracle: matcher\n"
    "  rule_author: skills/rules\n"
    "  source_session_id: synthetic-001\n"
    "  expect:\n"
    "    - tool_call: Bash\n"
    '      args.run_in_background: ~ "(?i)true"\n'
)

_JUDGE_LABEL = (
    "- entry_id: sample_judge\n"
    "  labelled_by: human:reviewer\n"
    '  labelled_at: "2026-06-09"\n'
    "  expected_behavior: explanation is faithful\n"
    "  outcome_axis: faithfulness\n"
    "  expected_outcome: faithful\n"
    "  confidence: medium\n"
    "  oracle: judge\n"
    "  judge:\n"
    "    rubric: the explanation matches the diff\n"
)


def _entry_id(label: str) -> str:
    for line in label.splitlines():
        if line.lstrip("- ").startswith("entry_id:"):
            return line.split(":", 1)[1].strip()
    pytest.fail("label fixture has no entry_id")


def _write_entry(directory: Path, name: str, *, label: str, session: str = '{"type":"user"}\n') -> Path:
    (directory / f"{_entry_id(label)}.session.jsonl").write_text(session, encoding="utf-8")
    path = directory / f"{name}.label.yaml"
    path.write_text(label, encoding="utf-8")
    return path


class TestDiscoverCorpus:
    def test_discovers_seed_entries(self) -> None:
        labels = discover_corpus()
        assert labels, "the shipped corpus must contain at least one seed entry"
        assert all(label.entry_id for label in labels)
        ids = [label.entry_id for label in labels]
        assert len(ids) == len(set(ids)), "shipped corpus has duplicate entry_ids"

    def test_at_least_one_matcher_oracle_entry(self) -> None:
        labels = discover_corpus()
        assert any(label.oracle == "matcher" for label in labels)

    def test_walks_a_custom_directory(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "sample_matcher", label=_MATCHER_LABEL)
        labels = discover_corpus(tmp_path)
        assert [label.entry_id for label in labels] == ["sample_matcher"]
        assert isinstance(labels[0].matchers[0], Matcher)

    def test_rejects_duplicate_entry_id(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, "a", label=_MATCHER_LABEL)
        _write_entry(tmp_path, "b", label=_MATCHER_LABEL)
        with pytest.raises(EvalSpecError, match="duplicate entry_id"):
            discover_corpus(tmp_path)


class TestLoadCorpusLabel:
    def test_loads_matcher_label(self, tmp_path: Path) -> None:
        path = _write_entry(tmp_path, "sample_matcher", label=_MATCHER_LABEL)
        label = load_corpus_label(path)
        assert label.entry_id == "sample_matcher"
        assert label.labelled_by == "human:reviewer"
        assert label.outcome_axis == "backgrounding"
        assert label.expected_outcome == "backgrounded"
        assert label.confidence == "high"
        assert label.oracle == "matcher"
        assert label.rule_author == "skills/rules"
        assert isinstance(label.matchers[0], Matcher)
        assert label.judge is None

    def test_loads_judge_label(self, tmp_path: Path) -> None:
        path = _write_entry(tmp_path, "sample_judge", label=_JUDGE_LABEL)
        label = load_corpus_label(path)
        assert label.oracle == "judge"
        assert label.matchers == ()
        assert label.judge is not None
        assert "matches the diff" in label.judge.rubric

    def test_rejects_matcher_oracle_with_no_matchers(self, tmp_path: Path) -> None:
        label = _MATCHER_LABEL.replace(
            '  expect:\n    - tool_call: Bash\n      args.run_in_background: ~ "(?i)true"\n', ""
        )
        path = _write_entry(tmp_path, "sample_matcher", label=label)
        with pytest.raises(EvalSpecError, match="oracle 'matcher'"):
            load_corpus_label(path)

    def test_rejects_judge_oracle_with_no_rubric(self, tmp_path: Path) -> None:
        label = _JUDGE_LABEL.replace("  judge:\n    rubric: the explanation matches the diff\n", "")
        path = _write_entry(tmp_path, "sample_judge", label=label)
        with pytest.raises(EvalSpecError, match="oracle 'judge'"):
            load_corpus_label(path)

    def test_rejects_missing_session_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "sample_matcher.label.yaml"
        path.write_text(_MATCHER_LABEL, encoding="utf-8")
        with pytest.raises(EvalSpecError, match="missing session jsonl"):
            load_corpus_label(path)

    def test_rejects_unknown_confidence(self, tmp_path: Path) -> None:
        label = _MATCHER_LABEL.replace("confidence: high", "confidence: certain")
        path = _write_entry(tmp_path, "sample_matcher", label=label)
        with pytest.raises(EvalSpecError, match="confidence"):
            load_corpus_label(path)

    def test_rejects_unknown_oracle(self, tmp_path: Path) -> None:
        label = _MATCHER_LABEL.replace("oracle: matcher", "oracle: vibes")
        path = _write_entry(tmp_path, "sample_matcher", label=label)
        with pytest.raises(EvalSpecError, match="oracle"):
            load_corpus_label(path)

    def test_rejects_non_list_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.label.yaml"
        path.write_text("entry_id: x\n", encoding="utf-8")
        (tmp_path / "bad.session.jsonl").write_text("{}\n", encoding="utf-8")
        with pytest.raises(EvalSpecError, match="single-entry YAML list"):
            load_corpus_label(path)

    def test_both_oracle_requires_matchers_and_judge(self, tmp_path: Path) -> None:
        label = _MATCHER_LABEL.replace("oracle: matcher", "oracle: both")
        path = _write_entry(tmp_path, "sample_matcher", label=label)
        with pytest.raises(EvalSpecError, match="oracle 'both'"):
            load_corpus_label(path)

    def test_rejects_malformed_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "sample_matcher.label.yaml"
        path.write_text("- entry_id: [unterminated\n", encoding="utf-8")
        (tmp_path / "sample_matcher.session.jsonl").write_text("{}\n", encoding="utf-8")
        with pytest.raises(EvalSpecError):
            load_corpus_label(path)

    def test_rejects_empty_required_field(self, tmp_path: Path) -> None:
        label = _MATCHER_LABEL.replace("labelled_by: human:reviewer", 'labelled_by: ""')
        path = _write_entry(tmp_path, "sample_matcher", label=label)
        with pytest.raises(EvalSpecError, match="labelled_by"):
            load_corpus_label(path)
