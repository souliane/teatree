import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

import teatree.cli as teatree_cli
from teatree.cli import app
from teatree.cli.tools import ToolRunner
from teatree.repo_mode import RepoMode

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

    def test_ai_sig_scan_wires_the_scanner_script(self):
        with patch.object(ToolRunner, "run_script") as mock:
            result = runner.invoke(app, ["tool", "ai-sig-scan", "body.txt"])
            assert result.exit_code == 0
            mock.assert_called_once_with("ai_signature_scan", "body.txt")

    def test_diff_coverage_passes_exit_zero(self, tmp_path):
        with (
            patch("teatree.utils.git.full_worktree_diff", return_value=""),
            patch("teatree.utils.diff_coverage.measure_diff_coverage") as mock,
        ):
            mock.return_value = MagicMock(passes=lambda: True, summary=lambda: "clean")
            result = runner.invoke(app, ["tool", "diff-coverage", "--repo", str(tmp_path)])
        assert result.exit_code == 0

    def test_diff_coverage_fails_exit_one_and_reports(self, tmp_path):
        report = MagicMock(
            passes=lambda: False,
            uncovered=[MagicMock(path="src/x.py", lines=[3, 4])],
            unreferenced_symbols=["widget"],
        )
        with (
            patch("teatree.utils.git.full_worktree_diff", return_value="diff"),
            patch("teatree.utils.diff_coverage.measure_diff_coverage", return_value=report),
        ):
            result = runner.invoke(app, ["tool", "diff-coverage", "--repo", str(tmp_path), "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["passes"] is False
        assert payload["unreferenced_symbols"] == ["widget"]
        assert payload["uncovered"] == [{"path": "src/x.py", "lines": [3, 4]}]

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


class TestRepoModeCommand:
    """``t3 tool repo-mode`` wires the resolver to plain/JSON output and flags."""

    def test_plain_output_is_the_bare_mode(self):
        with patch("teatree.repo_mode.resolve_repo_mode") as mock:
            mock.return_value = RepoMode.SOLO
            result = runner.invoke(app, ["tool", "repo-mode", "/some/repo"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "solo"
        mock.assert_called_once_with("/some/repo", refresh=False)

    def test_json_output_is_machine_readable(self):
        with patch("teatree.repo_mode.resolve_repo_mode") as mock:
            mock.return_value = RepoMode.COLLABORATIVE
            result = runner.invoke(app, ["tool", "repo-mode", "/some/repo", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"repo": "/some/repo", "mode": "collaborative"}

    def test_refresh_flag_is_forwarded(self):
        with patch("teatree.repo_mode.resolve_repo_mode") as mock:
            mock.return_value = RepoMode.SOLO
            runner.invoke(app, ["tool", "repo-mode", ".", "--refresh"])
        mock.assert_called_once_with(".", refresh=True)


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


class TestValidateMrCommand:
    """`t3 tool validate-mr` runs the active overlay's validate_pr (#119 Part 3).

    This is the command the pre-push hook invokes by default so a bad MR
    title/description is rejected BEFORE push, with no env-var opt-in.
    """

    def _overlay(self, errors: list[str], warnings: list[str] | None = None):
        ov = MagicMock()
        ov.metadata.validate_pr.return_value = {"errors": errors, "warnings": warnings or []}
        return ov

    def test_valid_metadata_exits_zero(self):
        with patch("teatree.cli.tools.get_overlay", return_value=self._overlay([])):
            result = runner.invoke(
                app,
                ["tool", "validate-mr", "--title", "fix: x (p#1)", "--description", "fix: x (p#1)\n\nB"],
            )
        assert result.exit_code == 0, result.output

    def test_invalid_metadata_exits_nonzero_and_prints_errors(self):
        ov = self._overlay(["Title is empty.", "MR description is empty."])
        with patch("teatree.cli.tools.get_overlay", return_value=ov):
            result = runner.invoke(app, ["tool", "validate-mr", "--title", "", "--description", ""])
        assert result.exit_code != 0
        assert "Title is empty." in result.output
        assert "MR description is empty." in result.output

    def test_passes_title_and_description_through_to_overlay(self):
        ov = self._overlay([])
        with patch("teatree.cli.tools.get_overlay", return_value=ov):
            runner.invoke(
                app,
                ["tool", "validate-mr", "--title", "feat: a [f] (p#1)", "--description", "body here"],
            )
        ov.metadata.validate_pr.assert_called_once_with("feat: a [f] (p#1)", "body here")
