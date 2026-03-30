"""Tests for CI-related CLI commands extracted from test_cli.py."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from teetree.cli import app
from teetree.cli_ci import CICommands

runner = CliRunner()


# ── CICommands.get_ci_service() ──────────────────────────────────────


class TestGetCIService:
    def test_from_env(self, monkeypatch):
        """Creates service from env when Django fails."""
        monkeypatch.setenv("TEATREE_GITLAB_TOKEN", "token")
        with patch("teetree.backends.loader.get_ci_service", side_effect=Exception("no django")):
            service = CICommands.get_ci_service()
            assert service is not None

    def test_no_token(self, monkeypatch):
        monkeypatch.delenv("TEATREE_GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch("teetree.backends.loader.get_ci_service", side_effect=Exception("no django")):
            assert CICommands.get_ci_service() is None


# ── CICommands.get_ci_project() ──────────────────────────────────────


class TestGetCIProject:
    def test_from_overlay(self):
        """Returns overlay path when available."""
        mock_overlay = MagicMock()
        mock_overlay.get_ci_project_path.return_value = "org/repo"
        with (
            patch("django.setup"),
            patch("teetree.core.overlay_loader.get_overlay", return_value=mock_overlay),
        ):
            result = CICommands.get_ci_project()
            assert result == "org/repo"

    def test_fallback_to_remote(self):
        """Falls back to git remote."""
        mock_project_info = MagicMock(path_with_namespace="org/repo-from-remote")
        with (
            patch("django.setup", side_effect=Exception("no django")),
            patch("teetree.utils.gitlab_api.GitLabAPI") as mock_api_cls,
        ):
            mock_api_cls.return_value.resolve_project_from_remote.return_value = mock_project_info
            result = CICommands.get_ci_project()
            assert result == "org/repo-from-remote"

    def test_no_remote(self):
        """Returns empty string when no remote."""
        with (
            patch("django.setup", side_effect=Exception("no django")),
            patch("teetree.utils.gitlab_api.GitLabAPI") as mock_api_cls,
        ):
            mock_api_cls.return_value.resolve_project_from_remote.return_value = None
            result = CICommands.get_ci_project()
            assert result == ""

    def test_overlay_returns_empty(self):
        """Falls back to remote when overlay returns empty path."""
        mock_overlay = MagicMock()
        mock_overlay.get_ci_project_path.return_value = ""
        mock_project_info = MagicMock(path_with_namespace="org/fallback")
        with (
            patch("django.setup"),
            patch("teetree.core.overlay_loader.get_overlay", return_value=mock_overlay),
            patch("teetree.utils.gitlab_api.GitLabAPI") as mock_api_cls,
        ):
            mock_api_cls.return_value.resolve_project_from_remote.return_value = mock_project_info
            result = CICommands.get_ci_project()
            assert result == "org/fallback"


# ── CICommands.current_git_branch() ──────────────────────────────────


class TestCurrentGitBranch:
    def test_success(self):
        with patch("teetree.cli_ci.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="feature-branch\n", returncode=0)
            assert CICommands.current_git_branch() == "feature-branch"

    def test_failure(self):
        with patch("teetree.cli_ci.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=128)
            assert CICommands.current_git_branch() == ""


# ── _require_ci helper ────────────────────────────────────────────────


class TestRequireCI:
    def test_cancel_no_service(self, monkeypatch):
        """Cancel fails without CI service."""
        monkeypatch.delenv("TEATREE_GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch("teetree.cli_ci.CICommands.get_ci_service", return_value=None):
            result = runner.invoke(app, ["ci", "cancel"])
            assert result.exit_code == 1
            assert "No CI service" in result.output

    def test_fetch_errors_no_service(self):
        with patch("teetree.cli_ci.CICommands.get_ci_service", return_value=None):
            result = runner.invoke(app, ["ci", "fetch-errors"])
            assert result.exit_code == 1

    def test_fetch_failed_tests_no_service(self):
        with patch("teetree.cli_ci.CICommands.get_ci_service", return_value=None):
            result = runner.invoke(app, ["ci", "fetch-failed-tests"])
            assert result.exit_code == 1

    def test_trigger_e2e_no_service(self):
        with patch("teetree.cli_ci.CICommands.get_ci_service", return_value=None):
            result = runner.invoke(app, ["ci", "trigger-e2e"])
            assert result.exit_code == 1

    def test_quality_check_no_service(self):
        with patch("teetree.cli_ci.CICommands.get_ci_service", return_value=None):
            result = runner.invoke(app, ["ci", "quality-check"])
            assert result.exit_code == 1


# ── CLI command wrappers ──────────────────────────────────────────────


class TestCICommands:
    def test_cancel_no_branch(self, monkeypatch):
        """Cancel fails when branch cannot be detected."""
        mock_ci = MagicMock()
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value=""),
        ):
            result = runner.invoke(app, ["ci", "cancel"])
            assert result.exit_code == 1
            assert "Could not detect branch" in result.output

    def test_cancel_with_results(self):
        """Cancel shows cancelled pipelines."""
        mock_ci = MagicMock()
        mock_ci.cancel_pipelines.return_value = [123, 456]
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "cancel"])
            assert result.exit_code == 0
            assert "Cancelled 2" in result.output

    def test_cancel_no_pipelines(self):
        """Cancel shows message when no pipelines found."""
        mock_ci = MagicMock()
        mock_ci.cancel_pipelines.return_value = []
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "cancel"])
            assert result.exit_code == 0
            assert "No running/pending" in result.output

    def test_cancel_with_explicit_branch(self):
        """Cancel uses explicit branch argument."""
        mock_ci = MagicMock()
        mock_ci.cancel_pipelines.return_value = [1]
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
        ):
            result = runner.invoke(app, ["ci", "cancel", "my-branch"])
            assert result.exit_code == 0
            mock_ci.cancel_pipelines.assert_called_once_with(project="org/repo", ref="my-branch")

    def test_divergence(self, monkeypatch, tmp_path):
        """Divergence shows ahead/behind counts."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        with (
            patch("teetree.utils.git.run", side_effect=["", "3", "1"]),
            patch("teetree.utils.git.current_branch", return_value="feature-branch"),
        ):
            result = runner.invoke(app, ["ci", "divergence"])
            assert result.exit_code == 0
            assert "3 ahead" in result.output
            assert "1 behind" in result.output

    def test_divergence_no_upstream(self, monkeypatch, tmp_path):
        """Divergence fails when no upstream configured."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        with patch("teetree.utils.git.run", side_effect=Exception("no upstream")):
            result = runner.invoke(app, ["ci", "divergence"])
            assert result.exit_code == 1
            assert "No upstream" in result.output

    def test_fetch_errors_with_errors(self):
        """Fetch-errors shows error logs."""
        mock_ci = MagicMock()
        mock_ci.fetch_pipeline_errors.return_value = ["Error in job build", "Error in job test"]
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "fetch-errors"])
            assert result.exit_code == 0
            assert "Error in job build" in result.output

    def test_fetch_errors_no_errors(self):
        """Fetch-errors shows clean message."""
        mock_ci = MagicMock()
        mock_ci.fetch_pipeline_errors.return_value = []
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "fetch-errors"])
            assert result.exit_code == 0
            assert "No errors found" in result.output

    def test_fetch_failed_tests_with_failures(self):
        """Fetch-failed-tests shows failed test IDs."""
        mock_ci = MagicMock()
        mock_ci.fetch_failed_tests.return_value = ["test_foo", "test_bar"]
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "fetch-failed-tests"])
            assert result.exit_code == 0
            assert "Failed tests (2)" in result.output
            assert "test_foo" in result.output

    def test_fetch_failed_tests_none(self):
        mock_ci = MagicMock()
        mock_ci.fetch_failed_tests.return_value = []
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "fetch-failed-tests"])
            assert result.exit_code == 0
            assert "No failed tests" in result.output

    def test_trigger_e2e_success(self):
        """Trigger-e2e triggers pipeline."""
        mock_ci = MagicMock()
        mock_ci.trigger_pipeline.return_value = {"web_url": "https://ci/pipeline/1"}
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "trigger-e2e"])
            assert result.exit_code == 0
            assert "Pipeline triggered" in result.output

    def test_trigger_e2e_error(self):
        mock_ci = MagicMock()
        mock_ci.trigger_pipeline.return_value = {"error": "forbidden"}
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value="main"),
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
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "quality-check"])
            assert result.exit_code == 0
            assert "Pipeline 42" in result.output
            assert "Failed: 2" in result.output

    def test_quality_check_error(self):
        mock_ci = MagicMock()
        mock_ci.quality_check.return_value = {"error": "no pipeline"}
        with (
            patch("teetree.cli_ci.CICommands.get_ci_service", return_value=mock_ci),
            patch("teetree.cli_ci.CICommands.get_ci_project", return_value="org/repo"),
            patch("teetree.cli_ci.CICommands.current_git_branch", return_value="main"),
        ):
            result = runner.invoke(app, ["ci", "quality-check"])
            assert result.exit_code == 1
