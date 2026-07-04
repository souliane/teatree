import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import teatree.cli as teatree_cli
from scripts.privacy_scan import PRIVACY_FINDINGS_EXIT_CODE
from teatree.cli import app
from teatree.cli.enforcement_tools import _coverage_is_stale
from teatree.cli.tools import ToolRunner
from teatree.core.overlay import OverlayBase, OverlayMetadata
from teatree.repo_mode import RepoMode

runner = CliRunner()

_GIT = shutil.which("git") or "git"


def _unified_diff(path: str, added: list[str]) -> str:
    """A minimal unified diff adding ``added`` lines to ``path``."""
    header = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1,1 +1,1 @@"]
    return "\n".join(header + [f"+{line}" for line in added]) + "\n"


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
            patch("teatree.utils.git.branch_diff", return_value=""),
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
            patch("teatree.utils.git.branch_diff", return_value="diff"),
            patch("teatree.utils.diff_coverage.measure_diff_coverage", return_value=report),
        ):
            result = runner.invoke(app, ["tool", "diff-coverage", "--repo", str(tmp_path), "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["passes"] is False
        assert payload["unreferenced_symbols"] == ["widget"]
        assert payload["uncovered"] == [{"path": "src/x.py", "lines": [3, 4]}]

    def test_diff_coverage_warns_on_stderr_when_coverage_absent(self, tmp_path):
        # Cold-review finding 5: an absent .coverage must produce a
        # visible stderr WARNING (the line-coverage half silently
        # measured nothing) without changing exit semantics.
        report = MagicMock(passes=lambda: True, summary=lambda: "clean")
        with (
            patch("teatree.utils.git.branch_diff", return_value=""),
            patch("teatree.utils.diff_coverage.measure_diff_coverage", return_value=report),
        ):
            absent = str(tmp_path / "absent.coverage")
            result = runner.invoke(
                app,
                ["tool", "diff-coverage", "--repo", str(tmp_path), "--coverage-file", absent],
                catch_exceptions=False,
            )
        assert result.exit_code == 0  # exit semantics unchanged
        assert "WARNING" in result.stderr
        assert "coverage" in result.stderr.lower()

    def test_diff_coverage_no_warning_when_coverage_present(self, tmp_path):
        cov = tmp_path / ".coverage"
        cov.write_text("", encoding="utf-8")
        report = MagicMock(passes=lambda: True, summary=lambda: "clean")
        with (
            patch("teatree.utils.git.branch_diff", return_value=""),
            patch("teatree.utils.diff_coverage.measure_diff_coverage", return_value=report),
        ):
            result = runner.invoke(
                app,
                ["tool", "diff-coverage", "--repo", str(tmp_path), "--coverage-file", str(cov)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "WARNING" not in result.stderr

    def test_diff_coverage_warns_when_coverage_stale(self, tmp_path):
        src = tmp_path / "src.py"
        src.write_text("x = 1\n", encoding="utf-8")
        cov = tmp_path / ".coverage"
        cov.write_text("", encoding="utf-8")
        past = time.time() - 10
        os.utime(cov, (past, past))
        report = MagicMock(passes=lambda: True, summary=lambda: "clean")
        with (
            patch("teatree.utils.git.branch_diff", return_value="diff --git a/src.py b/src.py\n+x = 1"),
            patch("teatree.utils.diff_coverage.measure_diff_coverage", return_value=report),
        ):
            result = runner.invoke(
                app,
                [
                    "tool",
                    "diff-coverage",
                    "--repo",
                    str(tmp_path),
                    "--coverage-file",
                    str(cov),
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "WARNING" in result.stderr
        assert "stale" in result.stderr.lower()

    def test_coverage_is_stale_degrades_when_file_removed_mid_walk(self, tmp_path):
        """A source file removed between rglob and stat must not crash the gate.

        ``_coverage_is_stale`` walks every ``*.py`` and stats it; a file
        vanishing mid-walk (a concurrent worktree prune, an editor swap) made
        ``stat`` raise ``FileNotFoundError`` and crash ``diff-coverage``. The
        per-file skip degrades to "not stale" for the vanished file instead.
        """
        cov = tmp_path / ".coverage"
        cov.write_text("", encoding="utf-8")
        os.utime(cov, (time.time() - 10, time.time() - 10))
        ghost = tmp_path / "ghost.py"
        ghost.write_text("x = 1\n", encoding="utf-8")

        real_stat = Path.stat

        def stat_raising_for_ghost(self, *args, **kwargs):
            if self.name == "ghost.py":
                raise FileNotFoundError(self)
            return real_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", stat_raising_for_ghost):
            assert _coverage_is_stale(cov, tmp_path) is False

    def test_coverage_is_stale_skips_only_vanished_file(self, tmp_path):
        """A vanished file is skipped; a present newer file still flags stale."""
        cov = tmp_path / ".coverage"
        cov.write_text("", encoding="utf-8")
        os.utime(cov, (time.time() - 10, time.time() - 10))
        ghost = tmp_path / "ghost.py"
        ghost.write_text("x = 1\n", encoding="utf-8")
        fresh = tmp_path / "fresh.py"
        fresh.write_text("y = 2\n", encoding="utf-8")  # newer than .coverage

        real_stat = Path.stat

        def stat_raising_for_ghost(self, *args, **kwargs):
            if self.name == "ghost.py":
                raise FileNotFoundError(self)
            return real_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", stat_raising_for_ghost):
            assert _coverage_is_stale(cov, tmp_path) is True

    def test_gate_relaxation_clean_staged_diff_passes(self, tmp_path):
        clean = _unified_diff("src/teatree/m.py", ["    x = 1"])
        with patch("teatree.utils.git_run.run", return_value=clean) as mock:
            result = runner.invoke(app, ["tool", "gate-relaxation", "--repo", str(tmp_path)])
        assert result.exit_code == 0
        assert "PASS" in result.stdout
        assert mock.call_args.kwargs["args"][:2] == ["diff", "--cached"]

    def test_gate_relaxation_staged_noqa_blocks_with_json_findings(self, tmp_path):
        dirty = _unified_diff("src/teatree/m.py", ["    x = bad()  # noqa"])
        with patch("teatree.utils.git_run.run", return_value=dirty):
            result = runner.invoke(app, ["tool", "gate-relaxation", "--repo", str(tmp_path), "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["passes"] is False
        assert any(f["kind"] == "noqa_without_justification" for f in payload["findings"])

    def test_gate_relaxation_base_vacuous_test_warns_but_passes(self, tmp_path):
        warn_diff = _unified_diff("tests/test_x.py", ["def test_it():", "    y = compute()"])
        with patch("teatree.utils.git_commit.branch_diff", return_value=warn_diff) as mock:
            result = runner.invoke(app, ["tool", "gate-relaxation", "--repo", str(tmp_path), "--base", "origin/main"])
        assert result.exit_code == 0
        assert "PASS" in result.stdout
        assert "WARN" in result.stderr
        assert "tests/test_x.py" in result.stderr
        mock.assert_called_once_with(str(tmp_path), "origin/main")

    def test_analyze_video(self):
        with patch.object(ToolRunner, "run_script") as mock:
            result = runner.invoke(app, ["tool", "analyze-video", "/path/to/video.mp4"])
            assert result.exit_code == 0
            mock.assert_called_once_with("analyze_video", "/path/to/video.mp4")

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_analyze_video_script_extracts_frames(self, tmp_path):
        """The script actually decomposes a video into frames when invoked.

        Regression for the script having no ``__main__`` entrypoint: it
        imported cleanly, exited 0, and produced zero frames — silently
        defeating every caller. Drives the real script over a generated
        test clip rather than mocking ``run_script``.
        """
        ffmpeg = shutil.which("ffmpeg")
        assert ffmpeg is not None
        video = tmp_path / "clip.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=4:size=320x240:rate=24",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                str(video),
                "-y",
            ],
            check=True,
        )
        out_dir = tmp_path / "frames"
        script = ToolRunner.scripts_dir() / "analyze_video.py"
        result = subprocess.run(
            [sys.executable, str(script), str(video), "--output", str(out_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        frames = sorted(out_dir.glob("frame_*.png"))
        assert frames, f"no frames extracted\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        assert "Frames extracted:" in result.stdout

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
        "    import sys; sys.exit(getattr(e, 'exit_code', 99))\n"
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
        # The dedicated findings exit code (#126) propagates through
        # ``t3 tool privacy-scan`` → ``run_script`` → ``typer.Exit``.
        assert code == PRIVACY_FINDINGS_EXIT_CODE, out
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

    def test_runs_to_completion_in_a_fresh_shell_without_django_preset(self) -> None:
        # Bug 4 (#126): the pre-push hook shells ``t3 tool validate-mr`` from
        # a session shell with no ``DJANGO_SETTINGS_MODULE``. ``get_overlay()``
        # imports the overlay's Django models, which raised
        # ``ImproperlyConfigured`` without ``django.setup()`` first — the gate
        # then failed CLOSED and blocked every MR/PR create. The in-process
        # tests above mock ``get_overlay`` and never hit that import, so the
        # real ``t3`` entrypoint is driven here in a subprocess with the env
        # var stripped, exactly as the hook invokes it.
        driver = (
            "import sys\n"
            "from teatree.cli import main\n"
            "sys.argv = ['t3', 'tool', 'validate-mr', '--title', 'feat: x (#1)', "
            "'--description', 'feat: x (#1)\\n\\n## What\\nx\\n\\n## Why\\ny']\n"
            "main()\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        proc = subprocess.run(
            [sys.executable, "-c", driver],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
        )
        assert proc.returncode == 0, f"validate-mr crashed:\n{proc.stdout}\n{proc.stderr}"
        assert "ImproperlyConfigured" not in proc.stderr
        assert "AppRegistryNotReady" not in proc.stderr


class _OverlayMeta(OverlayMetadata):
    def __init__(self, errors: list[str]) -> None:
        self._errors = errors

    def validate_pr(self, title: str, description: str):
        del title, description
        return {"errors": list(self._errors), "warnings": []}


class _AcceptingOverlay(OverlayBase):
    """Overlay whose ``validate_pr`` always passes, owning fixed repo slugs."""

    def __init__(self, repos: list[str]) -> None:
        self._repos = repos
        self.metadata = _OverlayMeta([])

    def get_repos(self) -> list[str]:
        return self._repos

    def get_provision_steps(self, worktree):
        return []


class _RejectingOverlay(OverlayBase):
    """Overlay whose ``validate_pr`` always rejects with fixed errors."""

    def __init__(self, repos: list[str], errors: list[str]) -> None:
        self._repos = repos
        self.metadata = _OverlayMeta(errors)

    def get_repos(self) -> list[str]:
        return self._repos

    def get_provision_steps(self, worktree):
        return []


class TestValidateMrMultipleOverlays:
    """`t3 tool validate-mr` must not crash when >1 overlay is registered (#1526).

    Before the fix ``validate_mr`` called ``get_overlay()`` with no name; with
    two overlays installed and no ``T3_OVERLAY_NAME`` that raised
    ``ImproperlyConfigured``. The pre-push hook treated the traceback (exit 1)
    as a "metadata invalid" verdict and hard-blocked every MR create/update —
    a lockout. The command now resolves the overlay by repo, and when still
    ambiguous validates leniently (PASS if ANY overlay accepts) so an advisory
    metadata check never hard-denies on ambiguity.

    Real overlay instances are registered via ``_discover_overlays`` (the live
    registry both ``get_overlay`` and ``get_all_overlays`` route through);
    nothing about overlay resolution is mocked.
    """

    def _register(self, monkeypatch, overlays: dict):
        # The test suite pins T3_OVERLAY_NAME=t3-teatree for determinism; drop
        # it so the ambiguity path (multiple overlays, no explicit name) runs.
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        return patch("teatree.core.overlay_loader._discover_overlays", return_value=overlays)

    def test_valid_metadata_exits_zero_with_two_overlays(self, monkeypatch):
        # RED before the fix: crashes ImproperlyConfigured (exit != 0).
        overlays = {
            "alpha": _AcceptingOverlay(["acme/alpha"]),
            "bravo": _AcceptingOverlay(["acme/bravo"]),
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(
                app,
                ["tool", "validate-mr", "--title", "fix: x", "--description", "fix: x\n\nbody"],
            )
        assert result.exit_code == 0, result.output
        assert "ImproperlyConfigured" not in result.output

    def test_resolvable_by_repo_uses_that_overlays_verdict(self, monkeypatch, tmp_path):
        # cwd repo belongs to exactly one overlay -> use ITS verdict, including
        # the deny path (no regression in rejection when resolution is sharp).
        repo = tmp_path / "alpha"
        repo.mkdir()
        subprocess.run([_GIT, "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run([_GIT, "remote", "add", "origin", "git@github.com:acme/alpha.git"], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        overlays = {
            "alpha": _RejectingOverlay(["acme/alpha"], ["Title is invalid."]),
            "bravo": _AcceptingOverlay(["acme/bravo"]),
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(app, ["tool", "validate-mr", "--title", "bad", "--description", "bad"])
        assert result.exit_code == 1
        assert "Title is invalid." in result.output

    def test_lenient_pass_when_any_overlay_accepts(self, monkeypatch):
        # Unresolvable by repo + multiple overlays: PASS if ANY accepts.
        overlays = {
            "alpha": _RejectingOverlay(["acme/alpha"], ["nope"]),
            "bravo": _AcceptingOverlay(["acme/bravo"]),
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(app, ["tool", "validate-mr", "--title", "fix: x", "--description", "fix: x"])
        assert result.exit_code == 0, result.output

    def test_deny_when_all_overlays_reject(self, monkeypatch):
        overlays = {
            "alpha": _RejectingOverlay(["acme/alpha"], ["alpha rejects"]),
            "bravo": _RejectingOverlay(["acme/bravo"], ["bravo rejects"]),
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(app, ["tool", "validate-mr", "--title", "bad", "--description", "bad"])
        assert result.exit_code == 1
        assert "alpha rejects" in result.output or "bravo rejects" in result.output

    def test_no_overlays_skips_fail_open(self, monkeypatch):
        with self._register(monkeypatch, {}):
            result = runner.invoke(app, ["tool", "validate-mr", "--title", "anything", "--description", "x"])
        assert result.exit_code == 0


class _CrashingOverlay(OverlayBase):
    """Overlay owning fixed repos whose ``validate_pr`` raises (loader can't grade)."""

    def __init__(self, repos: list[str]) -> None:
        self._repos = repos
        self.metadata = _OverlayMeta([])

    def get_repos(self) -> list[str]:
        return self._repos

    def get_provision_steps(self, worktree):
        return []


class _CrashingMeta(OverlayMetadata):
    def validate_pr(self, title: str, description: str):
        del title, description
        msg = "validator import boom"
        raise RuntimeError(msg)


class TestValidateMrTargetRepoKeyed:
    """`t3 tool validate-mr --repo <slug>` resolves the overlay from the MR TARGET.

    The cwd-keyed resolution validates an MR against whatever overlay owns the
    *current directory*. For a dispatched agent whose cwd is the more-lenient
    overlay's clone, that lenient overlay grades the MR — even when the MR
    targets a STRICTER overlay (one requiring, say, a trailing ``(url)``
    parenthetical). ``--repo`` keys resolution to the MR's target repo so the
    target overlay's rules apply regardless of cwd. ``strict-org/widget`` stands
    for the stricter target overlay; ``lenient-org/tool`` for the cwd overlay.
    """

    def _register(self, monkeypatch, overlays: dict):
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        return patch("teatree.core.overlay_loader._discover_overlays", return_value=overlays)

    def test_target_repo_overlay_denies_even_when_another_overlay_would_accept(self, monkeypatch):
        # The compounding bug: the any-overlay-pass fallback let a title pass
        # because the lenient overlay accepts it. Keyed to the strict target,
        # ONLY the strict overlay's verdict counts — no any-pass escape.
        overlays = {
            "lenient": _AcceptingOverlay(["lenient-org/tool"]),
            "strict": _RejectingOverlay(["strict-org/widget"], ["missing (url)"]),
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(
                app,
                [
                    "tool",
                    "validate-mr",
                    "--title",
                    "fix(x): missing url",
                    "--description",
                    "fix(x): missing url",
                    "--repo",
                    "strict-org/widget",
                ],
            )
        assert result.exit_code == 1, result.output
        assert "missing (url)" in result.output

    def test_compliant_title_targeting_strict_repo_is_allowed(self, monkeypatch):
        overlays = {
            "lenient": _AcceptingOverlay(["lenient-org/tool"]),
            "strict": _AcceptingOverlay(["strict-org/widget"]),
        }
        compliant = "fix(x): real change (https://example.com/strict-org/bugs/-/work_items/42)"
        with self._register(monkeypatch, overlays):
            result = runner.invoke(
                app,
                [
                    "tool",
                    "validate-mr",
                    "--title",
                    compliant,
                    "--description",
                    compliant,
                    "--repo",
                    "strict-org/widget",
                ],
            )
        assert result.exit_code == 0, result.output

    def test_known_target_overlay_whose_validator_crashes_fails_closed(self, monkeypatch):
        crashing = _CrashingOverlay(["strict-org/widget"])
        crashing.metadata = _CrashingMeta()
        overlays = {
            "lenient": _AcceptingOverlay(["lenient-org/tool"]),
            "strict": crashing,
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(
                app,
                [
                    "tool",
                    "validate-mr",
                    "--title",
                    "x",
                    "--description",
                    "x",
                    "--repo",
                    "strict-org/widget",
                ],
            )
        assert result.exit_code == 1, result.output

    def test_unmatched_target_repo_falls_back_to_cwd_behaviour(self, monkeypatch):
        # A target that maps to no overlay must NOT hard-deny — fall back to the
        # cwd-keyed (here ambiguous-lenient) resolution, preserving never-lockout.
        overlays = {
            "alpha": _RejectingOverlay(["acme/alpha"], ["nope"]),
            "bravo": _AcceptingOverlay(["acme/bravo"]),
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(
                app,
                [
                    "tool",
                    "validate-mr",
                    "--title",
                    "fix: x",
                    "--description",
                    "fix: x",
                    "--repo",
                    "unknown-org/unknown-repo",
                ],
            )
        assert result.exit_code == 0, result.output

    def test_lenient_target_does_not_require_strict_url(self, monkeypatch):
        # No regression: an MR targeting the lenient overlay validates against
        # the lenient rules, which do NOT demand the strict (url) parenthetical.
        overlays = {
            "lenient": _AcceptingOverlay(["lenient-org/tool"]),
            "strict": _RejectingOverlay(["strict-org/widget"], ["missing (url)"]),
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(
                app,
                [
                    "tool",
                    "validate-mr",
                    "--title",
                    "fix: lenient change",
                    "--description",
                    "fix: lenient change",
                    "--repo",
                    "lenient-org/tool",
                ],
            )
        assert result.exit_code == 0, result.output


class TestValidateMrTeatreeRepoNeverGetsExternalConvention:
    """`t3 tool validate-mr --repo souliane/teatree` always uses teatree's convention.

    When ``get_overlay_for_repo("souliane/teatree")`` returns ``None`` (the
    registered overlay set doesn't own that repo), the command must NOT fall
    through to the cwd-keyed ``get_overlay()`` path — that path could return an
    unrelated overlay whose narrower title-type set rejects titles perfectly
    valid under teatree's broader convention. The fix returns early (skip) when
    ``--repo`` was provided but maps to no known overlay.
    """

    def _register(self, monkeypatch: pytest.MonkeyPatch, overlays: dict):
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        return patch("teatree.core.overlay_loader._discover_overlays", return_value=overlays)

    def test_chore_title_accepted_for_teatree_repo_even_when_strict_overlay_is_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlays = {
            "t3-teatree": _AcceptingOverlay(["souliane/teatree"]),
            "strict-overlay": _RejectingOverlay(["acme-org/acme-product"], ["MR title type 'chore' is not allowed"]),
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(
                app,
                [
                    "tool",
                    "validate-mr",
                    "--title",
                    "chore: fix typo in README",
                    "--description",
                    "chore: fix typo in README",
                    "--repo",
                    "souliane/teatree",
                ],
            )
        assert result.exit_code == 0, result.output

    def test_chore_title_allowed_for_teatree_repo_when_only_strict_overlay_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlays = {
            "strict-overlay": _RejectingOverlay(["acme-org/acme-product"], ["MR title type 'chore' is not allowed"]),
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(
                app,
                [
                    "tool",
                    "validate-mr",
                    "--title",
                    "chore: fix typo in README",
                    "--description",
                    "chore: fix typo in README",
                    "--repo",
                    "souliane/teatree",
                ],
            )
        assert result.exit_code == 0, result.output

    def test_chore_title_rejected_for_strict_repo_when_strict_overlay_is_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlays = {
            "t3-teatree": _AcceptingOverlay(["souliane/teatree"]),
            "strict-overlay": _RejectingOverlay(["acme-org/acme-product"], ["MR title type 'chore' is not allowed"]),
        }
        with self._register(monkeypatch, overlays):
            result = runner.invoke(
                app,
                [
                    "tool",
                    "validate-mr",
                    "--title",
                    "chore: fix typo",
                    "--description",
                    "chore: fix typo",
                    "--repo",
                    "acme-org/acme-product",
                ],
            )
        assert result.exit_code == 1, result.output
        assert "chore" in result.output

    def test_teatree_title_with_teatree_exclusive_type_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        overlays = {
            "t3-teatree": _AcceptingOverlay(["souliane/teatree"]),
            "strict-overlay": _RejectingOverlay(["acme-org/acme-product"], ["not allowed"]),
        }
        with self._register(monkeypatch, overlays):
            for title_type in ("docs", "test", "refactor"):
                result = runner.invoke(
                    app,
                    [
                        "tool",
                        "validate-mr",
                        "--title",
                        f"{title_type}: something",
                        "--description",
                        f"{title_type}: something",
                        "--repo",
                        "souliane/teatree",
                    ],
                )
                assert result.exit_code == 0, f"Expected {title_type}: to pass for souliane/teatree\n{result.output}"


class TestToMarkdownCommand:
    """`t3 tool to-markdown` converts an attachment to Markdown via markitdown.

    Degrades gracefully (exit 1 + install hint) when the optional extra is
    absent; the converted Markdown is emitted verbatim as untrusted data.
    """

    def test_emits_converted_markdown_to_stdout(self, tmp_path):
        sample = tmp_path / "spec.xlsx"
        sample.write_bytes(b"stub")
        converter = MagicMock()
        converter.convert_file.return_value = "# Pricing\n\n| Item | Price |"
        with patch("teatree.backends.markdown_conversion.MarkdownConverter", return_value=converter):
            result = runner.invoke(app, ["tool", "to-markdown", str(sample)])
        assert result.exit_code == 0, result.output
        assert "# Pricing" in result.stdout
        converter.convert_file.assert_called_once_with(sample)

    def test_missing_markitdown_exits_one_with_install_hint(self, tmp_path):
        from teatree.backends.markdown_conversion import MarkdownConverterUnavailableError  # noqa: PLC0415

        sample = tmp_path / "spec.pdf"
        sample.write_bytes(b"stub")
        converter = MagicMock()
        converter.convert_file.side_effect = MarkdownConverterUnavailableError("install markitdown[pdf,docx,xlsx,pptx]")
        with patch("teatree.backends.markdown_conversion.MarkdownConverter", return_value=converter):
            result = runner.invoke(app, ["tool", "to-markdown", str(sample)])
        assert result.exit_code == 1
        assert "markitdown[pdf,docx,xlsx,pptx]" in result.output

    def test_conversion_error_exits_one_with_message(self, tmp_path):
        from teatree.backends.markdown_conversion import MarkdownConversionError  # noqa: PLC0415

        sample = tmp_path / "spec.bin"
        sample.write_bytes(b"stub")
        converter = MagicMock()
        converter.convert_file.side_effect = MarkdownConversionError("Could not convert spec.bin to Markdown: boom")
        with patch("teatree.backends.markdown_conversion.MarkdownConverter", return_value=converter):
            result = runner.invoke(app, ["tool", "to-markdown", str(sample)])
        assert result.exit_code == 1
        assert "Could not convert spec.bin" in result.output

    def test_missing_file_exits_one(self, tmp_path):
        result = runner.invoke(app, ["tool", "to-markdown", str(tmp_path / "absent.pdf")])
        assert result.exit_code == 1
        assert "File not found" in result.output
