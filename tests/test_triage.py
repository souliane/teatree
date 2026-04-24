"""Tests for teatree.triage — label inference for GitHub issues."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from teatree.triage import LABEL_KEYWORDS, LabelSuggester, infer_labels


def _issue_fixture() -> list[dict]:
    return [
        {"number": 1, "title": "feat: add new tool command", "body": "", "labels": []},
        {"number": 2, "title": "Server crash on DB disconnect", "body": "", "labels": []},
        {"number": 3, "title": "Update README", "body": "", "labels": [{"name": "documentation"}]},
    ]


class TestInferLabels:
    def test_bug_keywords(self) -> None:
        assert "bug" in infer_labels("Server crash on startup", "")
        assert "bug" in infer_labels("Error when saving", "")
        assert "bug" in infer_labels("Broken foo flow", "")

    def test_enhancement_keywords(self) -> None:
        assert "enhancement" in infer_labels("feat: add new setting", "")
        assert "enhancement" in infer_labels("add tenant filter", "")
        assert "enhancement" in infer_labels("improve cold-start latency", "")

    def test_documentation_keywords(self) -> None:
        assert "documentation" in infer_labels("Update README", "")
        assert "documentation" in infer_labels("Add docs for X", "")

    def test_architecture_keywords(self) -> None:
        assert "architecture" in infer_labels("Refactor overlay loader", "")
        assert "architecture" in infer_labels("split god-module", "")

    def test_dashboard_keywords(self) -> None:
        assert "dashboard" in infer_labels("Add panel to dashboard", "")
        assert "dashboard" in infer_labels("New view in admin", "")

    def test_multiple_matches(self) -> None:
        labels = infer_labels("refactor: split dashboard panel", "")
        assert {"architecture", "dashboard"}.issubset(set(labels))

    def test_no_match_returns_empty(self) -> None:
        assert infer_labels("random title", "random body") == []

    def test_body_contributes(self) -> None:
        assert "bug" in infer_labels("Something", "The server crashes under load.")

    def test_case_insensitive(self) -> None:
        assert "bug" in infer_labels("CRASH on startup", "")

    def test_word_boundary(self) -> None:
        labels = infer_labels("make the UI addressable by screen readers", "")
        assert "enhancement" not in labels

    def test_label_keywords_populated(self) -> None:
        assert set(LABEL_KEYWORDS) == {"bug", "enhancement", "documentation", "architecture", "dashboard"}
        assert all(LABEL_KEYWORDS[label] for label in LABEL_KEYWORDS)


def _fake_list_result(issues: list[dict]):
    return SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0)


class TestLabelSuggester:
    def test_suggest_skips_already_labeled(self) -> None:
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = _fake_list_result(_issue_fixture())
            suggestions = LabelSuggester("souliane/teatree").collect_suggestions()

        assert {s.number for s in suggestions} == {1, 2}

    def test_suggest_returns_inferred_labels(self) -> None:
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = _fake_list_result(_issue_fixture())
            suggestions = LabelSuggester("souliane/teatree").collect_suggestions()

        by_number = {s.number: s for s in suggestions}
        assert "enhancement" in by_number[1].labels
        assert "bug" in by_number[2].labels

    def test_suggest_omits_issues_with_no_inferred_labels(self) -> None:
        fixture = [{"number": 99, "title": "blah blah", "body": "", "labels": []}]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = _fake_list_result(fixture)
            suggestions = LabelSuggester("souliane/teatree").collect_suggestions()

        assert suggestions == []

    def test_apply_shells_out_once_per_issue(self) -> None:
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = _fake_list_result(_issue_fixture())
            suggester = LabelSuggester("souliane/teatree")
            suggestions = suggester.collect_suggestions()
            suggester.apply(suggestions)

        # 1 list call + 2 edit calls
        assert mock_run.call_count == 3
        edit_calls = [call for call in mock_run.call_args_list if "edit" in call.args[0]]
        assert len(edit_calls) == 2
        numbers_edited = {call.args[0][call.args[0].index("edit") + 1] for call in edit_calls}
        assert numbers_edited == {"1", "2"}

    def test_gh_failure_returns_empty(self) -> None:
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="", stderr="gh: not found", returncode=1)
            suggestions = LabelSuggester("souliane/teatree").collect_suggestions()
        assert suggestions == []
