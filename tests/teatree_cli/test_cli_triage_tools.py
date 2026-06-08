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
