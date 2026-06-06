"""Tests for CI-related CLI commands extracted from test_cli.py."""

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import teatree.backends.gitlab.api as gitlab_api_mod
import teatree.core.backend_factory as backend_factory_mod
import teatree.core.overlay_loader as overlay_loader_mod
import teatree.utils.git as git_mod
import teatree.utils.run as utils_run_mod
from teatree.cli import app
from teatree.cli.ci import CICommands

runner = CliRunner()


# ── CICommands.get_ci_service() ──────────────────────────────────────


class TestGetCIService:
    def test_from_env(self, monkeypatch):
        """Creates service from env when overlay fails."""
        monkeypatch.setenv("GITLAB_TOKEN", "token")
        with patch.object(backend_factory_mod, "ci_service_from_overlay", side_effect=Exception("no django")):
            service = CICommands.get_ci_service()
            assert service is not None

    def test_no_token(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(backend_factory_mod, "ci_service_from_overlay", side_effect=Exception("no django")):
            assert CICommands.get_ci_service() is None


# ── CICommands.get_ci_project() ──────────────────────────────────────


class TestGetCIProject:
    def test_from_overlay(self):
        """Returns overlay path when available."""
        mock_overlay = MagicMock()
        mock_overlay.metadata.get_ci_project_path.return_value = "org/repo"
        with (
            patch("django.setup"),
            patch.object(overlay_loader_mod, "get_overlay", return_value=mock_overlay),
        ):
            result = CICommands.get_ci_project()
            assert result == "org/repo"

    def test_fallback_to_remote(self):
        """Falls back to git remote."""
        mock_project_info = MagicMock(path_with_namespace="org/repo-from-remote")
        with (
            patch("django.setup", side_effect=Exception("no django")),
            patch.object(gitlab_api_mod, "GitLabAPI") as mock_api_cls,
        ):
            mock_api_cls.return_value.resolve_project_from_remote.return_value = mock_project_info
            result = CICommands.get_ci_project()
            assert result == "org/repo-from-remote"

    def test_no_remote(self):
        """Returns empty string when no remote."""
        with (
            patch("django.setup", side_effect=Exception("no django")),
            patch.object(gitlab_api_mod, "GitLabAPI") as mock_api_cls,
        ):
            mock_api_cls.return_value.resolve_project_from_remote.return_value = None
            result = CICommands.get_ci_project()
            assert result == ""

    def test_overlay_returns_empty(self):
        """Falls back to remote when overlay returns empty path."""
        mock_overlay = MagicMock()
        mock_overlay.metadata.get_ci_project_path.return_value = ""
        mock_project_info = MagicMock(path_with_namespace="org/fallback")
        with (
            patch("django.setup"),
            patch.object(overlay_loader_mod, "get_overlay", return_value=mock_overlay),
            patch.object(gitlab_api_mod, "GitLabAPI") as mock_api_cls,
        ):
            mock_api_cls.return_value.resolve_project_from_remote.return_value = mock_project_info
            result = CICommands.get_ci_project()
            assert result == "org/fallback"


# ── CICommands.current_git_branch() ──────────────────────────────────


class TestCurrentGitBranch:
    def test_success(self):
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stdout="feature-branch\n", returncode=0)
            assert CICommands.current_git_branch() == "feature-branch"

    def test_failure(self):
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=128)
            assert CICommands.current_git_branch() == ""


# ── _require_ci helper ────────────────────────────────────────────────


class TestRequireCI:
    def test_cancel_no_service(self, monkeypatch):
        """Cancel fails without CI service."""
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(CICommands, "get_ci_service", return_value=None):
            result = runner.invoke(app, ["ci", "cancel"])
            assert result.exit_code == 1
            assert "No CI service" in result.output

    def test_fetch_errors_no_service(self):
        with patch.object(CICommands, "get_ci_service", return_value=None):
            result = runner.invoke(app, ["ci", "fetch-errors"])
            assert result.exit_code == 1

    def test_fetch_failed_tests_no_service(self):
        with patch.object(CICommands, "get_ci_service", return_value=None):
            result = runner.invoke(app, ["ci", "fetch-failed-tests"])
            assert result.exit_code == 1

    def test_trigger_e2e_no_service(self):
        with patch.object(CICommands, "get_ci_service", return_value=None):
            result = runner.invoke(app, ["ci", "trigger-e2e"])
            assert result.exit_code == 1

    def test_quality_check_no_service(self):
        with patch.object(CICommands, "get_ci_service", return_value=None):
            result = runner.invoke(app, ["ci", "quality-check"])
            assert result.exit_code == 1


# ── CLI command wrappers ──────────────────────────────────────────────


class TestCICommands:
    def test_cancel_no_branch(self, monkeypatch):
        """Cancel fails when branch cannot be detected."""
        mock_ci = MagicMock()
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value=""),
        ):
            result = runner.invoke(app, ["ci", "cancel"])
            assert result.exit_code == 1
            assert "Could not detect branch" in result.output

    def test_cancel_with_results(self):
        """Cancel shows cancelled pipelines."""
        mock_ci = MagicMock()
        mock_ci.cancel_pipelines.return_value = [123, 456]
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "cancel"])
            assert result.exit_code == 0
            assert "Cancelled 2" in result.output

    def test_cancel_no_pipelines(self):
        """Cancel shows message when no pipelines found."""
        mock_ci = MagicMock()
        mock_ci.cancel_pipelines.return_value = []
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "cancel"])
            assert result.exit_code == 0
            assert "No running/pending" in result.output

    def test_cancel_with_explicit_branch(self):
        """Cancel uses explicit branch argument."""
        mock_ci = MagicMock()
        mock_ci.cancel_pipelines.return_value = [1]
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
        ):
            result = runner.invoke(app, ["ci", "cancel", "my-branch"])
            assert result.exit_code == 0
            mock_ci.cancel_pipelines.assert_called_once_with(project="org/repo", ref="my-branch")

    def test_divergence(self, monkeypatch, tmp_path):
        """Divergence shows ahead/behind counts."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        with (
            patch.object(git_mod, "run", side_effect=["", "3", "1"]),
            patch.object(git_mod, "current_branch", return_value="feature-branch"),
        ):
            result = runner.invoke(app, ["ci", "divergence"])
            assert result.exit_code == 0
            assert "3 ahead" in result.output
            assert "1 behind" in result.output

    def test_divergence_no_upstream(self, monkeypatch, tmp_path):
        """Divergence fails when no upstream configured."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        with patch.object(git_mod, "run", side_effect=Exception("no upstream")):
            result = runner.invoke(app, ["ci", "divergence"])
            assert result.exit_code == 1
            assert "No upstream" in result.output

    def test_fetch_errors_with_errors(self):
        """Fetch-errors shows error logs."""
        mock_ci = MagicMock()
        mock_ci.fetch_pipeline_errors.return_value = ["Error in job build", "Error in job test"]
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "fetch-errors"])
            assert result.exit_code == 0
            assert "Error in job build" in result.output

    def test_fetch_errors_no_errors(self):
        """Fetch-errors shows clean message."""
        mock_ci = MagicMock()
        mock_ci.fetch_pipeline_errors.return_value = []
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "fetch-errors"])
            assert result.exit_code == 0
            assert "No errors found" in result.output

    def test_fetch_failed_tests_with_failures(self):
        """Fetch-failed-tests shows failed test IDs."""
        mock_ci = MagicMock()
        mock_ci.fetch_failed_tests.return_value = ["test_foo", "test_bar"]
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "fetch-failed-tests"])
            assert result.exit_code == 0
            assert "Failed tests (2)" in result.output
            assert "test_foo" in result.output

    def test_fetch_failed_tests_none(self):
        mock_ci = MagicMock()
        mock_ci.fetch_failed_tests.return_value = []
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "fetch-failed-tests"])
            assert result.exit_code == 0
            assert "No failed tests" in result.output

    def test_trigger_e2e_success(self):
        """Trigger-e2e triggers pipeline."""
        mock_ci = MagicMock()
        mock_ci.trigger_pipeline.return_value = {"web_url": "https://ci/pipeline/1"}
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "trigger-e2e"])
            assert result.exit_code == 0
            assert "Pipeline triggered" in result.output

    def test_trigger_e2e_error(self):
        mock_ci = MagicMock()
        mock_ci.trigger_pipeline.return_value = {"error": "forbidden"}
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "trigger-e2e"])
            assert result.exit_code == 1
            assert "forbidden" in result.output

    def test_quality_check_success(self):
        mock_ci = MagicMock()
        mock_ci.quality_check.return_value = {
            "pipeline_id": 42,
            "status": "success",
            "total_count": 100,
            "success_count": 98,
            "failed_count": 2,
        }
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "quality-check"])
            assert result.exit_code == 0
            assert "Pipeline 42" in result.output
            assert "Failed: 2" in result.output

    def test_quality_check_error(self):
        mock_ci = MagicMock()
        mock_ci.quality_check.return_value = {"error": "no pipeline"}
        with (
            patch.object(CICommands, "get_ci_service", return_value=mock_ci),
            patch.object(CICommands, "get_ci_project", return_value="org/repo"),
            patch.object(CICommands, "current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "quality-check"])
            assert result.exit_code == 1


# ── ci coverage ──────────────────────────────────────────────────────


class TestCICoverage:
    def test_prints_floor_when_no_coverage_file(self, monkeypatch, tmp_path):
        """When .coverage is absent, print the configured floor and exit non-zero."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.coverage.report]\nfail_under = 93\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["ci", "coverage"])
        assert "Coverage floor" in result.output
        assert "93" in result.output
        assert "not measured" in result.output.lower() or "no .coverage" in result.output.lower()
        assert result.exit_code == 1

    def test_passes_when_overall_above_floor(self, monkeypatch, tmp_path):
        """When the report passes, command exits 0."""
        import teatree.cli.ci as ci_mod  # noqa: PLC0415
        from teatree.utils.coverage_floor import CoverageReport  # noqa: PLC0415

        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.coverage.report]\nfail_under = 93\n",
            encoding="utf-8",
        )
        fake = CoverageReport(overall_percent=95.5, overall_floor=93, module_results=[])
        with patch.object(ci_mod, "measure_coverage", return_value=fake):
            result = runner.invoke(app, ["ci", "coverage"])
        assert result.exit_code == 0
        assert "95.5" in result.output
        assert "93" in result.output

    def test_fails_when_overall_below_floor(self, monkeypatch, tmp_path):
        import teatree.cli.ci as ci_mod  # noqa: PLC0415
        from teatree.utils.coverage_floor import CoverageReport  # noqa: PLC0415

        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.coverage.report]\nfail_under = 93\n",
            encoding="utf-8",
        )
        fake = CoverageReport(overall_percent=80.0, overall_floor=93, module_results=[])
        with patch.object(ci_mod, "measure_coverage", return_value=fake):
            result = runner.invoke(app, ["ci", "coverage"])
        assert result.exit_code == 1

    def test_fails_when_module_below_floor(self, monkeypatch, tmp_path):
        import teatree.cli.ci as ci_mod  # noqa: PLC0415
        from teatree.utils.coverage_floor import CoverageReport, ModuleCoverage  # noqa: PLC0415

        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.coverage.report]\nfail_under = 93\n",
            encoding="utf-8",
        )
        fake = CoverageReport(
            overall_percent=95.0,
            overall_floor=93,
            module_results=[ModuleCoverage(path="src/teatree/loop/persistence.py", floor=80, percent=50.0)],
        )
        with patch.object(ci_mod, "measure_coverage", return_value=fake):
            result = runner.invoke(app, ["ci", "coverage"])
        assert result.exit_code == 1
        assert "persistence.py" in result.output
        assert "50" in result.output

    def test_json_output(self, monkeypatch, tmp_path):
        import json  # noqa: PLC0415

        import teatree.cli.ci as ci_mod  # noqa: PLC0415
        from teatree.utils.coverage_floor import CoverageReport, ModuleCoverage  # noqa: PLC0415

        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.coverage.report]\nfail_under = 93\n",
            encoding="utf-8",
        )
        fake = CoverageReport(
            overall_percent=95.0,
            overall_floor=93,
            module_results=[ModuleCoverage(path="x.py", floor=80, percent=85.0)],
        )
        with patch.object(ci_mod, "measure_coverage", return_value=fake):
            result = runner.invoke(app, ["ci", "coverage", "--json"])
        assert result.exit_code == 0
        # Read stdout (not .output): Click 8.3 mixes stderr into .output, and
        # any stderr (e.g. a deprecation warning) would break json.loads. The
        # JSON payload is on stdout; the #719 fix keeps the update banner out.
        data = json.loads(result.stdout)
        assert data["overall_percent"] == pytest.approx(95.0)
        assert data["overall_floor"] == 93
        assert data["modules"][0]["path"] == "x.py"
