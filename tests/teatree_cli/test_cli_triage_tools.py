"""Tests for cli/triage_tools.py — issue label/dedup commands.

Mirrors the source split out of cli/tools.py; patch targets point at
``teatree.cli.triage_tools`` where ``LabelSuggester``/``DuplicateFinder``
are now imported.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app

runner = CliRunner()


class TestLabelIssues:
    def test_no_suggestions_prints_message(self):
        with patch("teatree.cli.triage_tools.LabelSuggester") as suggester_cls:
            suggester_cls.return_value.collect_suggestions.return_value = []
            result = runner.invoke(app, ["tool", "label-issues", "owner/repo"])

        assert result.exit_code == 0
        assert "No labelable issues" in result.output

    def test_lists_suggestions_without_apply(self):
        suggestion = type("S", (), {"number": 7, "title": "bug", "labels": ["bug"]})()
        with patch("teatree.cli.triage_tools.LabelSuggester") as suggester_cls:
            suggester_cls.return_value.collect_suggestions.return_value = [suggestion]
            result = runner.invoke(app, ["tool", "label-issues", "owner/repo"])

        assert result.exit_code == 0
        assert "#7 bug" in result.output
        assert "Re-run with --apply" in result.output
        suggester_cls.return_value.apply.assert_not_called()

    def test_apply_invokes_suggester(self):
        suggestion = type("S", (), {"number": 7, "title": "bug", "labels": ["bug"]})()
        with patch("teatree.cli.triage_tools.LabelSuggester") as suggester_cls:
            suggester_cls.return_value.collect_suggestions.return_value = [suggestion]
            suggester_cls.return_value.apply.return_value = []
            result = runner.invoke(app, ["tool", "label-issues", "owner/repo", "--apply"])

        assert result.exit_code == 0
        assert "Applied labels to 1" in result.output
        suggester_cls.return_value.apply.assert_called_once()


class TestFindDuplicates:
    def test_no_matches(self):
        with patch("teatree.cli.triage_tools.DuplicateFinder") as finder_cls:
            finder_cls.return_value.find.return_value = []
            result = runner.invoke(app, ["tool", "find-duplicates", "owner/repo"])

        assert result.exit_code == 0
        assert "No potential duplicates" in result.output

    def test_lists_matches(self):
        match = type(
            "M",
            (),
            {"score": 0.91, "a_number": 1, "a_title": "A", "b_number": 2, "b_title": "B"},
        )()
        with patch("teatree.cli.triage_tools.DuplicateFinder") as finder_cls:
            finder_cls.return_value.find.return_value = [match]
            result = runner.invoke(app, ["tool", "find-duplicates", "owner/repo", "--threshold", "0.5"])

        assert result.exit_code == 0
        assert "0.91" in result.output
        assert "#1 A" in result.output


class TestFailedMutationsAreReported:
    """``gh issue edit``/``close`` failures were discarded, so every issue read as changed.

    The operator saw "Applied labels to N issue(s)" over a run where `gh` refused every
    call — the backlog looked triaged and nothing had moved.
    """

    def test_label_apply_reports_only_what_landed_and_exits_nonzero(self):
        suggestion = type("S", (), {"number": 7, "title": "bug", "labels": ["bug"]})()
        with patch("teatree.cli.triage_tools.LabelSuggester") as suggester_cls:
            suggester_cls.return_value.collect_suggestions.return_value = [suggestion]
            suggester_cls.return_value.apply.return_value = [7]
            result = runner.invoke(app, ["tool", "label-issues", "owner/repo", "--apply"])

        assert result.exit_code == 1
        assert "Applied labels to 0" in result.output
        assert "FAILED to label 1 issue(s): #7" in result.output

    def test_close_resolved_reports_only_what_landed_and_exits_nonzero(self):
        resolved = type(
            "R",
            (),
            {"issue_number": 4, "issue_title": "t", "pr_number": 9, "pr_title": "p", "confidence": "high"},
        )()
        with patch("teatree.triage.TriageScanner") as scanner_cls:
            scanner_cls.return_value.find_resolved.return_value = [resolved]
            scanner_cls.return_value.close_resolved.return_value = [4]
            scanner_cls.return_value.find_stale.return_value = []
            result = runner.invoke(app, ["tool", "triage-issues", "owner/repo", "--close-resolved"])

        assert result.exit_code == 1
        assert "Closed 0 resolved issue(s)." in result.output
        assert "FAILED to close 1 issue(s): #4" in result.output

    def test_a_fully_successful_close_still_exits_zero(self):
        resolved = type(
            "R",
            (),
            {"issue_number": 4, "issue_title": "t", "pr_number": 9, "pr_title": "p", "confidence": "high"},
        )()
        with patch("teatree.triage.TriageScanner") as scanner_cls:
            scanner_cls.return_value.find_resolved.return_value = [resolved]
            scanner_cls.return_value.close_resolved.return_value = []
            scanner_cls.return_value.find_stale.return_value = []
            result = runner.invoke(app, ["tool", "triage-issues", "owner/repo", "--close-resolved"])

        assert result.exit_code == 0, result.output
        assert "Closed 1 resolved issue(s)." in result.output
