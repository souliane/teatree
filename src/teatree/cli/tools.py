"""Tool CLI commands — standalone utilities."""

import json
import os
import sys
from pathlib import Path

import typer

from teatree.core.overlay_loader import get_overlay
from teatree.utils.run import run_allowed_to_fail

tool_app = typer.Typer(no_args_is_help=True, help="Standalone utilities.")


def _ensure_django() -> None:
    """Set up Django before touching the overlay's models — mirrors sibling CLI modules.

    ``get_overlay()`` imports the active overlay package, whose module body
    defines Django models. Without ``django.setup()`` first, that import
    raises ``ImproperlyConfigured`` / ``AppRegistryNotReady``. The pre-push
    hook shells ``t3 tool validate-mr`` from a fresh session shell with no
    ``DJANGO_SETTINGS_MODULE`` preset, so the command must initialise Django
    itself — a crash here fails the gate CLOSED and blocks every MR/PR create.
    """
    import django  # noqa: PLC0415

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    django.setup()


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
        # ``run_allowed_to_fail`` captures the child's streams. Re-emit
        # them so scripted callers actually see the script's diagnostics
        # — without this, ``t3 tool privacy-scan`` exits non-zero with no
        # visible findings, defeating the gate it powers (#696).
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        if result.returncode != 0:
            raise typer.Exit(code=result.returncode)


@tool_app.command("privacy-scan")
def privacy_scan(
    path: str = typer.Argument("-", help="File or '-' for stdin"),
) -> None:
    """Scan text for privacy-sensitive patterns (emails, keys, IPs)."""
    ToolRunner.run_script("privacy_scan", path)


@tool_app.command("validate-mr")
def validate_mr(
    title: str = typer.Option("", "--title", help="MR/PR title"),
    description: str = typer.Option("", "--description", help="MR/PR description"),
) -> None:
    """Validate MR/PR title+description against the active overlay's rules.

    Runs the active overlay's ``validate_pr`` (the same verdict used by
    ``t3 <overlay> pr create``). Exits non-zero and prints each error when
    the metadata is invalid. The pre-push hook invokes this by default so a
    bad title/description is rejected BEFORE the push — no env-var opt-in
    (#119).
    """
    _ensure_django()
    result = get_overlay().metadata.validate_pr(title, description)
    errors = result.get("errors", [])
    if errors:
        for err in errors:
            typer.echo(err, err=True)
        raise typer.Exit(code=1)


@tool_app.command("repo-mode")
def repo_mode(
    repo: str = typer.Argument(".", help="Repo path (default: current directory)"),
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass the 7-day cache and re-detect."),
) -> None:
    """Report whether the repo is solo (fix proactively) or collaborative (flag, don't fix).

    One heuristic for every skill: ``git shortlog`` over the last 90 days on
    the default branch. A ``[teatree] repo_mode`` config value overrides the
    detection. Result is cached 7 days per repo.
    """
    from teatree.repo_mode import resolve_repo_mode  # noqa: PLC0415

    mode = resolve_repo_mode(repo, refresh=refresh)
    if json_output:
        typer.echo(json.dumps({"repo": repo, "mode": mode.value}))
        return
    typer.echo(mode.value)


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


@tool_app.command("audit-memory")
def audit_memory(
    *,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show matched patterns for each entry."),
) -> None:
    """Scan Claude memory files for entries that should be promoted to skills."""
    from teatree.memory_audit import scan_all  # noqa: PLC0415

    entries = scan_all()
    if not entries:
        typer.echo("No promotable memory entries found.")
        return

    by_skill: dict[str, list] = {}
    for entry in entries:
        by_skill.setdefault(entry.suggested_skill, []).append(entry)

    typer.echo(f"Found {len(entries)} promotable memory entries:\n")
    for skill, skill_entries in sorted(by_skill.items()):
        typer.echo(f"  → t3:{skill} ({len(skill_entries)} entries)")
        for entry in skill_entries:
            typer.echo(f"    {entry.name}  [{entry.entry_type}]  {entry.path}")
            if verbose:
                for pattern in entry.matched_patterns:
                    typer.echo(f"      matched: {pattern}")


@tool_app.command("notion-download")
def notion_download(
    url: str = typer.Argument(
        ...,
        help="Either the `file://%7B…%7D` src from `notion-fetch` (resolved "
        "automatically via Notion's API — no browser click needed) or a "
        "pre-signed file.notion.so URL.",
    ),
    dest: Path = typer.Option(Path(), "--dest", "-d", help="Destination directory."),
) -> None:
    """Download a Notion file attachment using the Brave browser session.

    Accepts the `file://`-prefixed reference string that `t3`'s notion-fetch
    emits for `<file>` blocks; the signed URL is resolved server-side, so no
    manual browser click is required.
    """
    import re  # noqa: PLC0415
    from urllib.parse import urlparse  # noqa: PLC0415

    from teatree.backends.notion import NotionFileRef, download_notion_file  # noqa: PLC0415

    ref = NotionFileRef.from_fetch_src(url)
    if ref is not None:
        filename = ref.filename
        out = dest / filename if dest.is_dir() else dest
        typer.echo(f"Resolving + downloading {filename} (via Notion API)...")
        result = download_notion_file(ref=ref, dest=out)
        typer.echo(f"Saved: {result} ({result.stat().st_size:,} bytes)")
        return

    parsed = urlparse(url)
    path_match = re.match(r"/f/f/[^/]+/[^/]+/(.+)", parsed.path)
    if not path_match:
        typer.echo(f"Cannot parse file URL or notion-fetch ref: {url}")
        raise typer.Exit(1)

    filename = path_match.group(1).split("?", 1)[0]
    out = dest / filename if dest.is_dir() else dest
    typer.echo(f"Downloading {filename}...")
    result = download_notion_file(url=url, dest=out)
    typer.echo(f"Saved: {result} ({result.stat().st_size:,} bytes)")
