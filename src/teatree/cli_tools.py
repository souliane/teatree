"""Tool CLI commands — standalone utilities."""

import json
import os
import subprocess  # noqa: S404
import sys
from pathlib import Path

import typer

tool_app = typer.Typer(no_args_is_help=True, help="Standalone utilities.")


class ToolRunner:
    """Script and tool execution helpers."""

    @staticmethod
    def scripts_dir() -> Path:
        """Locate the scripts/ directory relative to the teatree package."""
        return Path(__file__).resolve().parent.parent.parent / "scripts"

    @staticmethod
    def run_script(script_name: str, *args: str) -> None:
        """Run a script from the scripts/ directory."""
        scripts = ToolRunner.scripts_dir()
        script = scripts / f"{script_name}.py"
        if not script.is_file():
            typer.echo(f"Script not found: {script}")
            raise typer.Exit(code=1)
        cmd = [sys.executable, str(script), *args]
        result = subprocess.run(cmd, check=False)  # noqa: S603
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
    result = subprocess.run(cmd, check=False)  # noqa: S603
    raise typer.Exit(code=result.returncode)


@tool_app.command("claude-handover")
def claude_handover(
    *,
    current_runtime: str = typer.Option(
        "",
        help="Current CLI runtime. Defaults to the highest-priority configured runtime.",
    ),
    session_id: str = typer.Option("", help="Claude session ID to inspect. Defaults to latest telemetry."),
    state_dir: Path | None = typer.Option(None, help="Override the Claude statusline telemetry directory."),  # noqa: B008
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
        f"recommended={recommendation}"
    )
