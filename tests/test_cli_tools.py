import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli_tools import ToolRunner

runner = CliRunner()


class TestToolRunner:
    def test_scripts_dir_returns_path(self):
        result = ToolRunner.scripts_dir()
        assert isinstance(result, Path)
        assert result.name == "scripts"

    def test_run_script_success(self, tmp_path):
        """ToolRunner.run_script succeeds for a passing script."""
        script = tmp_path / "ok_script.py"
        script.write_text("pass")
        with patch("teatree.cli_tools.ToolRunner.scripts_dir", return_value=tmp_path):
            ToolRunner.run_script("ok_script")

    def test_run_script_failure(self, tmp_path):
        """ToolRunner.run_script raises Exit on non-zero returncode."""
        import click  # noqa: PLC0415

        script = tmp_path / "test_script.py"
        script.write_text("import sys; sys.exit(2)")
        with patch("teatree.cli_tools.ToolRunner.scripts_dir", return_value=tmp_path):
            try:
                ToolRunner.run_script("test_script")
                msg = "Expected Exit"
                raise AssertionError(msg)
            except (SystemExit, click.exceptions.Exit) as e:
                assert e.exit_code == 2  # noqa: PT017

    def test_run_script_not_found(self, tmp_path):
        """ToolRunner.run_script raises Exit when script not found."""
        import click  # noqa: PLC0415

        with patch("teatree.cli_tools.ToolRunner.scripts_dir", return_value=tmp_path):
            try:
                ToolRunner.run_script("nonexistent_script")
                msg = "Expected Exit"
                raise AssertionError(msg)
            except (SystemExit, click.exceptions.Exit) as e:
                assert e.exit_code == 1  # noqa: PT017


class TestToolCommands:
    def test_privacy_scan(self):
        with patch("teatree.cli_tools.ToolRunner.run_script") as mock:
            result = runner.invoke(app, ["tool", "privacy-scan", "myfile.txt"])
            assert result.exit_code == 0
            mock.assert_called_once_with("privacy_scan", "myfile.txt")

    def test_analyze_video(self):
        with patch("teatree.cli_tools.ToolRunner.run_script") as mock:
            result = runner.invoke(app, ["tool", "analyze-video", "/path/to/video.mp4"])
            assert result.exit_code == 0
            mock.assert_called_once_with("analyze_video", "/path/to/video.mp4")

    def test_bump_deps(self):
        with patch("teatree.cli_tools.ToolRunner.run_script") as mock:
            result = runner.invoke(app, ["tool", "bump-deps"])
            assert result.exit_code == 0
            mock.assert_called_once_with("bump-pyproject-deps-from-lock-file")


class TestSonarCheck:
    def test_script_not_found(self, tmp_path):
        """Sonar-check exits with error when script is missing."""
        with patch("teatree.cli._find_overlay_project", return_value=tmp_path):
            result = runner.invoke(app, ["tool", "sonar-check"])
            assert result.exit_code == 1
            assert "sonar_check.sh not found" in result.output

    def test_success(self, tmp_path):
        """Tool sonar-check calls the overlay script directly."""
        script = tmp_path / "scripts" / "sonar_check.sh"
        script.parent.mkdir()
        script.touch()
        with (
            patch("teatree.cli._find_overlay_project", return_value=tmp_path),
            patch("teatree.cli_tools.subprocess") as mock_sub,
        ):
            mock_sub.run.return_value = subprocess.CompletedProcess([], 0)
            result = runner.invoke(app, ["tool", "sonar-check", "/tmp/repo"])
            assert result.exit_code == 0
            args = mock_sub.run.call_args[0][0]
            assert args[0] == "bash"
            assert args[1] == str(script)
            assert "/tmp/repo" in args

    def test_with_flags(self, tmp_path):
        """Tool sonar-check passes skip-baseline and remote flags."""
        script = tmp_path / "scripts" / "sonar_check.sh"
        script.parent.mkdir()
        script.touch()
        with (
            patch("teatree.cli._find_overlay_project", return_value=tmp_path),
            patch("teatree.cli_tools.subprocess") as mock_sub,
        ):
            mock_sub.run.return_value = subprocess.CompletedProcess([], 0)
            result = runner.invoke(app, ["tool", "sonar-check", "--skip-baseline", "--remote", "--remote-status"])
            assert result.exit_code == 0
            args = mock_sub.run.call_args[0][0]
            assert "--skip-baseline" in args
            assert "--remote" in args
            assert "--remote-status" in args

    def test_uses_pwd_env(self, tmp_path, monkeypatch):
        """When no repo_path given, sonar-check uses $PWD (not os.getcwd())."""
        script = tmp_path / "scripts" / "sonar_check.sh"
        script.parent.mkdir()
        script.touch()
        monkeypatch.setenv("PWD", "/original/worktree")
        with (
            patch("teatree.cli._find_overlay_project", return_value=tmp_path),
            patch("teatree.cli_tools.subprocess") as mock_sub,
        ):
            mock_sub.run.return_value = subprocess.CompletedProcess([], 0)
            result = runner.invoke(app, ["tool", "sonar-check", "--remote"])
            assert result.exit_code == 0
            args = mock_sub.run.call_args[0][0]
            assert "/original/worktree" in args
