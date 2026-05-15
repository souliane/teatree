import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

import teatree.cli as teatree_cli
from teatree.cli import app
from teatree.cli.tools import ToolRunner

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
        with (
            patch.object(ToolRunner, "scripts_dir", return_value=tmp_path),
            patch("teatree.utils.run.subprocess") as mock_sp,
        ):
            mock_sp.run.return_value = subprocess.CompletedProcess([], 0)
            ToolRunner.run_script("ok_script")

    def test_run_script_failure(self, tmp_path):
        """ToolRunner.run_script raises Exit on non-zero returncode."""
        import click  # noqa: PLC0415

        script = tmp_path / "test_script.py"
        script.write_text("import sys; sys.exit(2)")
        with (
            patch.object(ToolRunner, "scripts_dir", return_value=tmp_path),
            patch("teatree.utils.run.subprocess") as mock_sp,
        ):
            mock_sp.run.return_value = subprocess.CompletedProcess([], 2)
            try:
                ToolRunner.run_script("test_script")
                msg = "Expected Exit"
                raise AssertionError(msg)
            except (SystemExit, click.exceptions.Exit) as e:
                assert e.exit_code == 2  # noqa: PT017

    def test_run_script_not_found(self, tmp_path):
        """ToolRunner.run_script raises Exit when script not found."""
        import click  # noqa: PLC0415

        with patch.object(ToolRunner, "scripts_dir", return_value=tmp_path):
            try:
                ToolRunner.run_script("nonexistent_script")
                msg = "Expected Exit"
                raise AssertionError(msg)
            except (SystemExit, click.exceptions.Exit) as e:
                assert e.exit_code == 1  # noqa: PT017


class TestToolCommands:
    def test_privacy_scan(self):
        with patch.object(ToolRunner, "run_script") as mock:
            result = runner.invoke(app, ["tool", "privacy-scan", "myfile.txt"])
            assert result.exit_code == 0
            mock.assert_called_once_with("privacy_scan", "myfile.txt")

    def test_analyze_video(self):
        with patch.object(ToolRunner, "run_script") as mock:
            result = runner.invoke(app, ["tool", "analyze-video", "/path/to/video.mp4"])
            assert result.exit_code == 0
            mock.assert_called_once_with("analyze_video", "/path/to/video.mp4")

    def test_bump_deps(self):
        with patch.object(ToolRunner, "run_script") as mock:
            result = runner.invoke(app, ["tool", "bump-deps"])
            assert result.exit_code == 0
            mock.assert_called_once_with("bump-pyproject-deps-from-lock-file")


class TestPrivacyScanWrapperSurfacesFindings:
    """``t3 tool privacy-scan`` must surface the scanner's findings (#696).

    ``ToolRunner.run_script`` spawns the scanner via
    ``run_allowed_to_fail`` which uses ``capture_output=True``. Before the
    fix the captured stdout/stderr were discarded, so a piped caller saw
    "exit 1, no output". The wrapper must re-emit what the scanner wrote so
    the finding (line/category/redacted match) is caller-visible. The real
    scanner subprocess is exercised here — nothing is mocked.
    """

    # ``ToolRunner.run_script`` spawns a fresh ``python`` child that reads
    # the *real* stdin, so the only faithful reproduction of the
    # ``printf ... | t3 tool privacy-scan -`` flow is a real subprocess
    # that drives ``run_script`` with piped stdin. Monkeypatching
    # ``sys.stdin`` in-process would not reach the grandchild scanner.
    _DRIVER = (
        "from teatree.cli.tools import ToolRunner\n"
        "try:\n"
        "    ToolRunner.run_script('privacy_scan', '-')\n"
        "except SystemExit as e:\n"
        "    raise\n"
        "except Exception as e:\n"
        "    import sys; sys.exit(getattr(e, 'exit_code', 3))\n"
    )

    def _invoke(self, stdin_text: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, "-c", self._DRIVER],
            input=stdin_text,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        return proc.returncode, proc.stdout + proc.stderr

    def test_planted_secret_finding_reaches_caller(self) -> None:
        code, out = self._invoke("token = glpat-XXXXXXXXXXXXXXXX\n")  # privacy-scan:allow self-fixture
        assert code == 1, out
        assert "api_key" in out
        assert "glpat-" in out

    def test_clean_input_reaches_caller(self) -> None:
        code, out = self._invoke("an ordinary line\n")
        assert code == 0, out
        assert "clean" in out.lower()


class TestSonarCheck:
    def test_script_not_found(self, tmp_path):
        """Sonar-check exits with error when script is missing."""
        with patch.object(teatree_cli, "_find_overlay_project", return_value=tmp_path):
            result = runner.invoke(app, ["tool", "sonar-check"])
            assert result.exit_code == 1
            assert "sonar_check.sh not found" in result.output

    def test_success(self, tmp_path):
        """Tool sonar-check calls the overlay script directly."""
        script = tmp_path / "scripts" / "sonar_check.sh"
        script.parent.mkdir()
        script.touch()
        with (
            patch.object(teatree_cli, "_find_overlay_project", return_value=tmp_path),
            patch("teatree.utils.run.subprocess") as mock_sub,
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
            patch.object(teatree_cli, "_find_overlay_project", return_value=tmp_path),
            patch("teatree.utils.run.subprocess") as mock_sub,
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
            patch.object(teatree_cli, "_find_overlay_project", return_value=tmp_path),
            patch("teatree.utils.run.subprocess") as mock_sub,
        ):
            mock_sub.run.return_value = subprocess.CompletedProcess([], 0)
            result = runner.invoke(app, ["tool", "sonar-check", "--remote"])
            assert result.exit_code == 0
            args = mock_sub.run.call_args[0][0]
            assert "/original/worktree" in args


class TestLabelIssues:
    def test_no_suggestions_prints_message(self):
        with patch("teatree.cli.tools.LabelSuggester") as suggester_cls:
            suggester_cls.return_value.collect_suggestions.return_value = []
            result = runner.invoke(app, ["tool", "label-issues", "owner/repo"])

        assert result.exit_code == 0
        assert "No labelable issues" in result.output

    def test_lists_suggestions_without_apply(self):
        suggestion = type("S", (), {"number": 7, "title": "bug", "labels": ["bug"]})()
        with patch("teatree.cli.tools.LabelSuggester") as suggester_cls:
            suggester_cls.return_value.collect_suggestions.return_value = [suggestion]
            result = runner.invoke(app, ["tool", "label-issues", "owner/repo"])

        assert result.exit_code == 0
        assert "#7 bug" in result.output
        assert "Re-run with --apply" in result.output
        suggester_cls.return_value.apply.assert_not_called()

    def test_apply_invokes_suggester(self):
        suggestion = type("S", (), {"number": 7, "title": "bug", "labels": ["bug"]})()
        with patch("teatree.cli.tools.LabelSuggester") as suggester_cls:
            suggester_cls.return_value.collect_suggestions.return_value = [suggestion]
            result = runner.invoke(app, ["tool", "label-issues", "owner/repo", "--apply"])

        assert result.exit_code == 0
        assert "Applied labels to 1" in result.output
        suggester_cls.return_value.apply.assert_called_once()


class TestFindDuplicates:
    def test_no_matches(self):
        with patch("teatree.cli.tools.DuplicateFinder") as finder_cls:
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
        with patch("teatree.cli.tools.DuplicateFinder") as finder_cls:
            finder_cls.return_value.find.return_value = [match]
            result = runner.invoke(app, ["tool", "find-duplicates", "owner/repo", "--threshold", "0.5"])

        assert result.exit_code == 0
        assert "0.91" in result.output
        assert "#1 A" in result.output
