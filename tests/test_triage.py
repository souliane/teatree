"""Tests for teatree.triage — label inference and duplicate detection for GitHub issues."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.tools import tool_app
from teatree.triage import LABEL_KEYWORDS, DuplicateFinder, LabelSuggester, TriageScanner, infer_labels, normalize_title


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

    def test_multiple_matches(self) -> None:
        labels = infer_labels("refactor: improve overlay loader feature", "")
        assert {"architecture", "enhancement"}.issubset(set(labels))

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
        assert set(LABEL_KEYWORDS) == {"bug", "enhancement", "documentation", "architecture"}
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


class TestNormalizeTitle:
    def test_lowercases(self) -> None:
        assert normalize_title("Fix BUG case") == normalize_title("fix bug case")

    def test_strips_conventional_prefix(self) -> None:
        assert normalize_title("feat: add a thing") == normalize_title("add a thing")
        assert normalize_title("fix(cli): add a thing") == normalize_title("add a thing")
        assert normalize_title("docs(scope)!: add a thing") == normalize_title("add a thing")

    def test_strips_pr_suffix(self) -> None:
        assert normalize_title("add a thing (#123)") == normalize_title("add a thing")

    def test_strips_punctuation(self) -> None:
        assert normalize_title("add a thing!") == normalize_title("add a thing")
        assert normalize_title("add, a thing.") == normalize_title("add a thing")

    def test_collapses_whitespace(self) -> None:
        assert normalize_title("add   a   thing") == normalize_title("add a thing")

    def test_strips_leading_emoji(self) -> None:
        # Common "noise" words we don't want to count as signal.
        assert normalize_title("[WIP] add a thing") == normalize_title("add a thing")


def _dup_issue(number: int, title: str, labels: list[dict] | None = None) -> dict:
    return {"number": number, "title": title, "body": "", "labels": labels or []}


class TestDuplicateFinder:
    def test_finds_near_identical_titles(self) -> None:
        issues = [
            _dup_issue(1, "Dashboard SSE disconnects under load"),
            _dup_issue(2, "Dashboard SSE disconnects under load."),
            _dup_issue(3, "Totally unrelated thing"),
        ]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = _fake_list_result(issues)
            matches = DuplicateFinder("souliane/teatree").find()

        assert len(matches) == 1
        pair = matches[0]
        assert {pair.a_number, pair.b_number} == {1, 2}
        assert pair.score >= 0.9

    def test_finds_conventional_commit_variants(self) -> None:
        issues = [
            _dup_issue(10, "feat: add duplicate detection"),
            _dup_issue(11, "Add duplicate detection"),
            _dup_issue(12, "fix: something else entirely"),
        ]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = _fake_list_result(issues)
            matches = DuplicateFinder("souliane/teatree").find()

        assert any({m.a_number, m.b_number} == {10, 11} for m in matches)

    def test_ignores_low_similarity(self) -> None:
        issues = [
            _dup_issue(1, "Dashboard SSE disconnects under load"),
            _dup_issue(2, "Switch CI from codecov to coveralls"),
        ]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = _fake_list_result(issues)
            matches = DuplicateFinder("souliane/teatree").find()

        assert matches == []

    def test_threshold_is_configurable(self) -> None:
        issues = [
            _dup_issue(1, "Refactor overlay loader"),
            _dup_issue(2, "Refactor overlay module"),
        ]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = _fake_list_result(issues)
            strict = DuplicateFinder("souliane/teatree", threshold=0.99).find()
            loose = DuplicateFinder("souliane/teatree", threshold=0.6).find()

        assert strict == []
        assert len(loose) >= 1

    def test_each_pair_reported_once(self) -> None:
        issues = [
            _dup_issue(1, "Duplicate issue detection"),
            _dup_issue(2, "Duplicate issue detection"),
            _dup_issue(3, "Duplicate issue detection"),
        ]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = _fake_list_result(issues)
            matches = DuplicateFinder("souliane/teatree").find()

        # With 3 identical titles we expect C(3, 2) = 3 unique pairs, no self-pairs, no duplicates.
        pair_keys = {frozenset((m.a_number, m.b_number)) for m in matches}
        assert len(pair_keys) == len(matches) == 3
        assert all(m.a_number != m.b_number for m in matches)

    def test_gh_failure_returns_empty(self) -> None:
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="", stderr="gh: not found", returncode=1)
            matches = DuplicateFinder("souliane/teatree").find()
        assert matches == []


def _pr_fixture(number: int, title: str) -> dict:
    return {"number": number, "title": title, "mergedAt": "2026-05-01T00:00:00Z"}


def _issue_with_age(number: int, title: str, *, labels: list[dict] | None = None, days_ago: int = 0) -> dict:
    updated = (datetime.now(tz=UTC) - timedelta(days=days_ago)).isoformat()
    return {"number": number, "title": title, "body": "", "labels": labels or [], "updatedAt": updated}


class TestTriageScanner:
    def test_finds_resolved_issues_by_title_reference(self) -> None:
        issues = [_issue_with_age(42, "feat: add triage tool")]
        prs = [_pr_fixture(100, "feat: add triage tool (#42)")]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.side_effect = [
                SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps(prs), stderr="", returncode=0),
            ]
            scanner = TriageScanner("souliane/teatree")
            resolved = scanner.find_resolved()
        assert len(resolved) == 1
        assert resolved[0].issue_number == 42
        assert resolved[0].pr_number == 100

    def test_ignores_issues_without_merged_pr(self) -> None:
        issues = [_issue_with_age(42, "feat: add triage tool")]
        prs: list[dict] = []
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.side_effect = [
                SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps(prs), stderr="", returncode=0),
            ]
            resolved = TriageScanner("souliane/teatree").find_resolved()
        assert resolved == []

    def test_finds_stale_issues(self) -> None:
        issues = [
            _issue_with_age(1, "Old issue", days_ago=60),
            _issue_with_age(2, "Recent issue", days_ago=5),
            _issue_with_age(3, "Another old one", days_ago=45),
        ]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0)
            stale = TriageScanner("souliane/teatree").find_stale(days=30)
        assert len(stale) == 2
        assert {s.issue_number for s in stale} == {1, 3}

    def test_stale_excludes_labeled_issues(self) -> None:
        issues = [
            _issue_with_age(1, "Old but labeled", labels=[{"name": "enhancement"}], days_ago=60),
            _issue_with_age(2, "Old and unlabeled", days_ago=60),
        ]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0)
            stale = TriageScanner("souliane/teatree").find_stale(days=30)
        assert len(stale) == 1
        assert stale[0].issue_number == 2

    def test_find_resolved_gh_pr_failure_returns_empty(self) -> None:
        issues = [_issue_with_age(42, "feat: thing")]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.side_effect = [
                SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="error", returncode=1),
            ]
            assert TriageScanner("souliane/teatree").find_resolved() == []

    def test_stale_skips_empty_updated_at(self) -> None:
        issues = [{"number": 1, "title": "No date", "body": "", "labels": [], "updatedAt": ""}]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0)
            assert TriageScanner("souliane/teatree").find_stale(days=1) == []

    def test_confidence_property(self) -> None:
        issues = [_issue_with_age(42, "feat: triage")]
        prs = [_pr_fixture(100, "feat: triage (#42)")]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.side_effect = [
                SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps(prs), stderr="", returncode=0),
            ]
            resolved = TriageScanner("souliane/teatree").find_resolved()
        assert resolved[0].confidence == "high"

    def test_gh_failure_returns_empty(self) -> None:
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="", stderr="error", returncode=1)
            assert TriageScanner("souliane/teatree").find_resolved() == []
            assert TriageScanner("souliane/teatree").find_stale() == []


runner = CliRunner()


class TestTriageIssuesCLI:
    def test_shows_resolved_and_stale(self) -> None:
        issues = [_issue_with_age(42, "feat: add triage tool", days_ago=60)]
        prs = [_pr_fixture(100, "feat: add triage tool (#42)")]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.side_effect = [
                SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps(prs), stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0),
            ]
            result = runner.invoke(tool_app, ["triage-issues", "souliane/teatree", "--stale-days", "30"])
        assert result.exit_code == 0
        assert "#42" in result.output
        assert "merged PR #100" in result.output
        assert "60d inactive" in result.output

    def test_no_findings(self) -> None:
        issues = [_issue_with_age(1, "Recent labeled", labels=[{"name": "bug"}], days_ago=1)]
        prs: list[dict] = []
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.side_effect = [
                SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps(prs), stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0),
            ]
            result = runner.invoke(tool_app, ["triage-issues", "souliane/teatree"])
        assert result.exit_code == 0
        assert "No resolved-but-open" in result.output
        assert "No stale" in result.output

    def test_close_resolved_flag(self) -> None:
        issues = [_issue_with_age(42, "feat: add triage tool")]
        prs = [_pr_fixture(100, "feat: add triage tool (#42)")]
        with patch("teatree.triage.run_allowed_to_fail") as mock_run:
            mock_run.side_effect = [
                SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps(prs), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps(issues), stderr="", returncode=0),
            ]
            result = runner.invoke(tool_app, ["triage-issues", "souliane/teatree", "--close-resolved"])
        assert result.exit_code == 0
        assert "Closed 1" in result.output
