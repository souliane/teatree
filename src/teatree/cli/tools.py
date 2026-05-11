"""Tool CLI commands — standalone utilities."""

import json
import os
import sys
from pathlib import Path

import typer

from teatree.triage import DuplicateFinder, LabelSuggester
from teatree.utils.run import run_allowed_to_fail

tool_app = typer.Typer(no_args_is_help=True, help="Standalone utilities.")


class ToolRunner:
    """Script and tool execution helpers."""

    @staticmethod
    def scripts_dir() -> Path:
        """Locate the scripts/ directory relative to the teatree package."""
        return Path(__file__).resolve().parent.parent.parent.parent / "scripts"

    @staticmethod
    def run_script(script_name: str, *args: str) -> None:
        """Run a script from the scripts/ directory."""
        scripts = ToolRunner.scripts_dir()
        script = scripts / f"{script_name}.py"
        if not script.is_file():
            typer.echo(f"Script not found: {script}")
            raise typer.Exit(code=1)
        cmd = [sys.executable, str(script), *args]
        result = run_allowed_to_fail(cmd, expected_codes=None)
        if result.returncode != 0:
            raise typer.Exit(code=result.returncode)


@tool_app.command("privacy-scan")
def privacy_scan(
    path: str = typer.Argument("-", help="File or '-' for stdin"),
) -> None:
    """Scan text for privacy-sensitive patterns (emails, keys, IPs)."""
    ToolRunner.run_script("privacy_scan", path)


@tool_app.command("analyze-video")
def analyze_video(
    video_path: str = typer.Argument(..., help="Path to video file"),
) -> None:
    """Decompose video into frames for AI analysis."""
    ToolRunner.run_script("analyze_video", video_path)


@tool_app.command("bump-deps")
def bump_deps() -> None:
    """Bump pyproject.toml dependencies from uv.lock."""
    ToolRunner.run_script("bump-pyproject-deps-from-lock-file")


@tool_app.command("sonar-check")
def sonar_check(
    repo_path: str = typer.Argument("", help="Path to repo (default: current directory)"),
    *,
    skip_baseline: bool = typer.Option(default=False, help="Reuse previous baseline"),
    remote: bool = typer.Option(default=False, help="Push to CI server instead of local"),
    remote_status: bool = typer.Option(default=False, help="Fetch CI Sonar results"),
) -> None:
    """Run local SonarQube analysis via Docker."""
    from teatree.cli import _find_overlay_project  # noqa: PLC0415

    project = _find_overlay_project()
    script = project / "scripts" / "sonar_check.sh"
    if not script.is_file():
        typer.echo(f"sonar_check.sh not found in {project / 'scripts'}")
        raise typer.Exit(code=1)
    cmd = ["bash", str(script)]
    if not repo_path:
        repo_path = os.environ.get("PWD", str(Path.cwd()))
    cmd.append(repo_path)
    if skip_baseline:
        cmd.append("--skip-baseline")
    if remote:
        cmd.append("--remote")
    if remote_status:
        cmd.append("--remote-status")
    result = run_allowed_to_fail(cmd, expected_codes=None)
    raise typer.Exit(code=result.returncode)


@tool_app.command("label-issues")
def label_issues(
    repo: str = typer.Argument(..., help="Repository in owner/name form (e.g. souliane/teatree)"),
    *,
    apply: bool = typer.Option(False, "--apply", help="Apply labels via `gh issue edit` (default: print only)."),
) -> None:
    """Suggest labels for unlabeled open issues by keyword-matching title and body."""
    suggester = LabelSuggester(repo)
    suggestions = suggester.collect_suggestions()
    if not suggestions:
        typer.echo("No labelable issues found.")
        return

    for s in suggestions:
        typer.echo(f"#{s.number} {s.title}  -> {', '.join(s.labels)}")

    if apply:
        suggester.apply(suggestions)
        typer.echo(f"Applied labels to {len(suggestions)} issue(s).")
    else:
        typer.echo(f"\n{len(suggestions)} issue(s) to label. Re-run with --apply to apply.")


@tool_app.command("find-duplicates")
def find_duplicates(
    repo: str = typer.Argument(..., help="Repository in owner/name form (e.g. souliane/teatree)"),
    *,
    threshold: float = typer.Option(
        0.75,
        "--threshold",
        min=0.0,
        max=1.0,
        help="Similarity ratio required to flag a pair (0.0-1.0).",
    ),
) -> None:
    """Flag pairs of open issues with near-identical titles."""
    finder = DuplicateFinder(repo, threshold=threshold)
    matches = finder.find()
    if not matches:
        typer.echo("No potential duplicates found.")
        return

    for match in matches:
        typer.echo(
            f"  {match.score:.2f}  #{match.a_number} {match.a_title}\n         #{match.b_number} {match.b_title}"
        )
    typer.echo(f"\n{len(matches)} potential duplicate pair(s).")


@tool_app.command("claude-handover")
def claude_handover(
    *,
    current_runtime: str = typer.Option(
        "",
        help="Current CLI runtime. Defaults to the highest-priority configured runtime.",
    ),
    session_id: str = typer.Option("", help="Claude session ID to inspect. Defaults to latest telemetry."),
    state_dir: Path | None = typer.Option(None, help="Override the Claude statusline telemetry directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show Claude handover telemetry and runtime recommendations."""
    from teatree.agents.handover import build_claude_handover_status  # noqa: PLC0415

    status = build_claude_handover_status(current_runtime=current_runtime, session_id=session_id, state_dir=state_dir)
    if json_output:
        typer.echo(json.dumps(status))
        return

    telemetry_state = "available" if status["telemetry_available"] else "missing"
    used = status["five_hour_used_percentage"]
    reset_at = status["five_hour_resets_at"] or "unknown"
    recommendation = status["recommended_runtime"] or "stay"
    typer.echo(
        "Claude handover telemetry: "
        f"current={status['current_runtime']}; "
        f"preferred={status['preferred_runtime']}; "
        f"{telemetry_state}; "
        f"5h={used if used is not None else 'n/a'}%; "
        f"reset={reset_at}; "
        f"recommended={recommendation}",
    )
