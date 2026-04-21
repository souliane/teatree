"""Tests for the ``t3 assess`` CLI commands."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import teatree.cli.assess as assess_mod
import teatree.utils.run as utils_run_mod
from teatree.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _suppress_update_check(monkeypatch):
    """Prevent the root callback update check from polluting CLI output."""
    monkeypatch.setattr("teatree.cli._maybe_show_update_notice", lambda: None)


class TestAssessRun:
    def test_skill_not_found(self):
        """Fails when ac-reviewing-codebase skill CLI is not installed."""
        with patch.object(assess_mod, "_find_skill_cli", return_value=None):
            result = runner.invoke(app, ["assess", "run"])
            assert result.exit_code == 1
            assert "skill not found" in result.output

    def test_subprocess_failure(self, tmp_path):
        """Fails when the skill CLI returns non-zero."""
        fake_cli = tmp_path / "cli.py"
        fake_cli.touch()
        with (
            patch.object(assess_mod, "_find_skill_cli", return_value=fake_cli),
            patch.object(utils_run_mod.subprocess, "run") as mock_run,
        ):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "ruff not found"
            result = runner.invoke(app, ["assess", "run", "--no-save"])
            assert result.exit_code == 1
            assert "Assessment failed" in result.output

    def test_invalid_json(self, tmp_path):
        """Fails when the skill CLI returns invalid JSON."""
        fake_cli = tmp_path / "cli.py"
        fake_cli.touch()
        with (
            patch.object(assess_mod, "_find_skill_cli", return_value=fake_cli),
            patch.object(utils_run_mod.subprocess, "run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "not json"
            result = runner.invoke(app, ["assess", "run", "--no-save"])
            assert result.exit_code == 1
            assert "Invalid JSON" in result.output

    def test_successful_run_json_output(self, tmp_path):
        """Outputs JSON metrics when --json flag is used."""
        fake_cli = tmp_path / "cli.py"
        fake_cli.touch()
        metrics = {"lint": {"total": 5}, "todos": {"total": 3}}
        with (
            patch.object(assess_mod, "_find_skill_cli", return_value=fake_cli),
            patch.object(utils_run_mod.subprocess, "run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(metrics)
            result = runner.invoke(app, ["assess", "run", "--json", "--no-save"])
            assert result.exit_code == 0
            parsed = json.loads(result.output)
            assert parsed["lint"]["total"] == 5

    def test_successful_run_human_output(self, tmp_path):
        """Prints human-readable summary by default."""
        fake_cli = tmp_path / "cli.py"
        fake_cli.touch()
        metrics = {
            "lint": {"total": 0},
            "todos": {"total": 2},
            "complexity": {"violations": 1},
            "coverage": {"available": True, "percent": 85.3},
            "dependencies": {"available": True, "outdated_count": 0},
            "suppressions": {"noqa": 3},
        }
        with (
            patch.object(assess_mod, "_find_skill_cli", return_value=fake_cli),
            patch.object(utils_run_mod.subprocess, "run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(metrics)
            result = runner.invoke(app, ["assess", "run", "--no-save"])
            assert result.exit_code == 0
            assert "Lint violations" in result.output
            assert "TODOs" in result.output
            assert "85.3%" in result.output

    def test_saves_assessment(self, tmp_path):
        """Saves assessment JSON to .t3/assessments/."""
        fake_cli = tmp_path / "cli.py"
        fake_cli.touch()
        metrics = {"lint": {"total": 0}, "todos": {"total": 0}}
        with (
            patch.object(assess_mod, "_find_skill_cli", return_value=fake_cli),
            patch.object(utils_run_mod.subprocess, "run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(metrics)
            result = runner.invoke(app, ["assess", "run", "--root", str(tmp_path)])
            assert result.exit_code == 0
            assert "Saved:" in result.output
            saved_files = list((tmp_path / ".t3" / "assessments").glob("*.json"))
            assert len(saved_files) == 1
            saved = json.loads(saved_files[0].read_text())
            assert saved["metrics"] == metrics


class TestAssessHistory:
    def test_no_assessments_dir(self, tmp_path):
        """Fails when no assessments directory exists."""
        result = runner.invoke(app, ["assess", "history", "--root", str(tmp_path)])
        assert result.exit_code == 1
        assert "No assessments found" in result.output

    def test_empty_assessments_dir(self, tmp_path):
        """Fails when assessments directory is empty."""
        (tmp_path / ".t3" / "assessments").mkdir(parents=True)
        result = runner.invoke(app, ["assess", "history", "--root", str(tmp_path)])
        assert result.exit_code == 1
        assert "No assessment files found" in result.output

    def test_shows_history(self, tmp_path):
        """Displays history table from saved assessments."""
        assessments_dir = tmp_path / ".t3" / "assessments"
        assessments_dir.mkdir(parents=True)
        data = {
            "date": "2026-04-07",
            "repo": "test-repo",
            "metrics": {
                "lint": {"total": 5},
                "todos": {"total": 3},
                "complexity": {"violations": 2},
                "coverage": {"available": True, "percent": 80.0},
                "dependencies": {"available": True, "outdated_count": 1},
                "suppressions": {"noqa": 2, "type_ignore": 1},
            },
        }
        (assessments_dir / "2026-04-07.json").write_text(json.dumps(data))
        result = runner.invoke(app, ["assess", "history", "--root", str(tmp_path)])
        assert result.exit_code == 0
        assert "2026-04-07" in result.output


class TestFindSkillCli:
    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        """Returns None when skill CLI is not in any known location."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = assess_mod._find_skill_cli()
        assert result is None

    def test_finds_in_claude_skills(self, tmp_path, monkeypatch):
        """Finds CLI in ~/.claude/skills/."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cli_path = tmp_path / ".claude" / "skills" / "ac-reviewing-codebase" / "scripts" / "cli.py"
        cli_path.parent.mkdir(parents=True)
        cli_path.touch()
        result = assess_mod._find_skill_cli()
        assert result == cli_path


class TestSaveAssessment:
    def test_creates_dir_and_writes(self, tmp_path):
        """Creates .t3/assessments/ and writes JSON."""
        metrics = {"lint": {"total": 0}}
        assess_mod._save_assessment(tmp_path, metrics)
        files = list((tmp_path / ".t3" / "assessments").glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["repo"] == tmp_path.name
        assert data["metrics"] == metrics


class TestPrintSummary:
    def test_handles_empty_metrics(self):
        """Doesn't crash on empty metrics dict."""
        assess_mod._print_summary({})

    def test_all_sections(self, capsys):
        """Prints all metric sections when available."""
        metrics = {
            "lint": {"total": 3},
            "todos": {"total": 5},
            "complexity": {"violations": 2},
            "coverage": {"available": True, "percent": 92.0},
            "dependencies": {"available": True, "outdated_count": 0},
            "suppressions": {"noqa": 1},
        }
        assess_mod._print_summary(metrics)
