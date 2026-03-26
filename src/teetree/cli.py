"""TeaTree CLI — single ``t3`` entry point for all commands.

DB-touching commands are django-typer management commands, exposed here after
``django.setup()``.  Django-free commands live as plain Typer groups.
"""

import json
import logging
import os
import shutil
import subprocess  # noqa: S404
import sys
from datetime import UTC
from pathlib import Path
from textwrap import dedent

import typer

logger = logging.getLogger(__name__)

app = typer.Typer(name="t3", no_args_is_help=True, add_completion=False)

# ── Always-available commands (no Django) ──────────────────────────────


@app.callback()
def _root_callback(ctx: typer.Context) -> None:
    ctx.ensure_object(dict)


@app.command()
def startproject(
    project_name: str,
    destination: Path,
    *,
    overlay_app: str = typer.Option(
        "t3_overlay", "--overlay-app", help="Name of the overlay Django app (t3_ prefix recommended)"
    ),
    project_package: str | None = typer.Option(
        None, "--project-package", help="Project package name (default: derived from project name)"
    ),
) -> None:
    """Create a new TeaTree overlay project."""
    project_root = destination / project_name
    if project_root.exists():
        typer.echo(f"Destination already exists: {project_root}")
        raise typer.Exit(code=1)

    package_name = project_package or project_name.replace("-", "_").replace("t3_", "")

    src_dir = project_root / "src"
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "django", "startproject", package_name, str(project_root)],
        check=True,
    )
    src_dir.mkdir()
    (project_root / package_name).rename(src_dir / package_name)

    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "django", "startapp", overlay_app],
        cwd=src_dir,
        check=True,
    )

    skill_base = overlay_app.removeprefix("t3_").removesuffix("_overlay") or "overlay"
    skill_name = f"t3-{skill_base.replace('_', '-')}"
    skill_dir = project_root / "skills" / skill_name
    skill_dir.mkdir(parents=True)

    overlay_class_name = _camelize(overlay_app)
    _patch_settings(src_dir / package_name / "settings.py", overlay_app, overlay_class_name)
    _patch_urls(src_dir / package_name / "urls.py")
    _write_overlay(src_dir / overlay_app / "overlay.py", overlay_app, overlay_class_name, skill_name)
    _write_skill_md(skill_dir / "SKILL.md", project_name, skill_name)
    _copy_config_templates(project_root)
    _write_pyproject(project_root, project_name, overlay_app, package_name)
    _patch_manage_py(project_root / "manage.py")
    (project_root / "manage.py").chmod(0o755)
    (project_root / ".env").write_text(f"DJANGO_SETTINGS_MODULE={package_name}.settings\n", encoding="utf-8")

    typer.echo(str(project_root))


@app.command()
def docs(
    host: str = typer.Option("127.0.0.1", help="Host to bind to"),
    port: int = typer.Option(8888, help="Port to serve on"),
) -> None:
    """Serve the project documentation with mkdocs.

    Requires the ``docs`` dependency group: ``uv sync --group docs``
    """
    project_root = _find_project_root()
    mkdocs_yml = project_root / "mkdocs.yml"
    if not mkdocs_yml.exists():
        typer.echo(f"No mkdocs.yml found in {project_root}")
        raise typer.Exit(code=1)
    try:
        import mkdocs  # noqa: F401, PLC0415
    except ImportError:
        typer.echo("mkdocs is not installed. Run: uv sync --group docs")
        raise typer.Exit(code=1) from None
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "mkdocs", "serve", "-a", f"{host}:{port}"],
        cwd=project_root,
        check=True,
    )


_TASK_PHASE_KEYWORDS: dict[str, list[str]] = {
    "t3-debug": ["debug", "fix", "error", "broken", "crash", "not working", "bug", "trace"],
    "t3-test": ["test", "pytest", "e2e", "lint", "ci", "pipeline", "qa"],
    "t3-ship": ["commit", "push", "ship", "deliver", "mr", "merge request", "pull request"],
    "t3-review": ["review", "feedback", "check the code"],
    "t3-ticket": ["ticket", "issue", "start working on"],
    "t3-retro": ["retro", "retrospective", "lessons learned"],
    "t3-workspace": ["setup", "worktree", "create worktree", "servers", "cleanup"],
}

_DEFAULT_SKILLS = ["t3-code", "t3-debug"]


def _resolve_agent_skills(
    task: str,
    project_root: Path,
) -> list[str]:
    """Build the skills list for ``t3 agent`` based on context.

    1. Discover overlay-specific skills from ``skills/`` directory.
    2. Pick t3 lifecycle skills from task keywords (or defaults).
    3. Deduplicate while preserving order.
    """
    skills: list[str] = []

    # Overlay skills from project's skills/ directory
    skills_dir = project_root / "skills"
    if skills_dir.is_dir():
        skills.extend(skill_md.parent.name for skill_md in sorted(skills_dir.glob("*/SKILL.md")))

    # Task-based lifecycle skill selection
    task_lower = task.lower()
    matched: list[str] = []
    if task_lower:
        for skill_name, keywords in _TASK_PHASE_KEYWORDS.items():
            if any(kw in task_lower for kw in keywords):
                matched.append(skill_name)

    lifecycle_skills = matched or list(_DEFAULT_SKILLS)

    for skill in lifecycle_skills:
        if skill not in skills:
            skills.append(skill)

    return skills


def _launch_claude(*, task: str, project_root: Path, context_lines: list[str]) -> None:
    """Shared logic: resolve skills, build prompt, exec into claude."""
    import shutil  # noqa: PLC0415

    claude_bin = shutil.which("claude")
    if not claude_bin:
        typer.echo("claude CLI not found on PATH. Install Claude Code first.")
        raise typer.Exit(code=1)

    skills = _resolve_agent_skills(task, project_root)

    teatree_editable, teatree_url = _editable_info("teatree")
    if teatree_editable and teatree_url:
        context_lines.append(f"TeaTree source (editable): {teatree_url.removeprefix('file://')}")
    context_lines.append("")

    from teetree.agents.skill_bundle import DEFAULT_SKILLS_DIR, resolve_dependencies  # noqa: PLC0415

    resolved = resolve_dependencies(skills, skills_dir=DEFAULT_SKILLS_DIR)
    context_lines.extend(
        (
            "BLOCKING REQUIREMENT: Read ALL skill files below BEFORE doing anything else.",
            "Do NOT skip. Do NOT start working until every file is read.",
        )
    )
    for skill_name in resolved:
        skill_md = DEFAULT_SKILLS_DIR / skill_name / "SKILL.md"
        if skill_md.is_file():  # pragma: no branch
            context_lines.append(f"  - {skill_md}")
    context_lines.extend(("", "Run `uv run t3 --help` to see available commands.", "Run `uv run pytest` to run tests."))
    if task:
        context_lines.extend(("", f"Task: {task}"))

    context = "\n".join(context_lines)
    cmd = [claude_bin, "--append-system-prompt", context]
    if task:
        cmd.extend(["-p", task])

    typer.echo(f"Launching Claude Code in {project_root}...")
    os.execvp(claude_bin, cmd)  # noqa: S606


@app.command()
def agent(
    task: str = typer.Argument("", help="What to work on (e.g. 'fix the sync bug', 'add a new command')"),
) -> None:
    """Launch Claude Code with auto-detected project context."""
    from teetree.config import discover_active_overlay  # noqa: PLC0415

    project_root = _find_project_root()
    active = discover_active_overlay()

    lines = ["You are working on a TeaTree project.", ""]
    if active:
        lines.extend(
            (
                f"Active overlay: {active.name} (settings: {active.settings_module})",
                f"Overlay source: {project_root}",
            )
        )
    else:
        lines.append("No overlay active — working on teatree itself.")

    _launch_claude(task=task, project_root=project_root, context_lines=lines)


@app.command()
def sessions(
    project: str = typer.Option("", help="Filter by project dir substring"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max sessions to show"),
    *,
    all_projects: bool = typer.Option(False, "--all", "-a", help="Show sessions from all projects"),
) -> None:
    """List recent Claude conversation sessions with resume commands.

    By default shows sessions for the current working directory.
    Use --all to show sessions across all projects.
    """
    from datetime import datetime  # noqa: PLC0415

    from teetree.claude_sessions import SessionQuery, list_sessions  # noqa: PLC0415

    results = list_sessions(
        SessionQuery(
            project_filter=project,
            all_projects=all_projects,
            limit=limit,
        )
    )

    if not results:
        typer.echo("No sessions found.")
        raise typer.Exit()

    now = datetime.now(tz=UTC).timestamp()
    for r in results:
        raw_ts = r.timestamp
        if isinstance(raw_ts, str):
            try:
                raw_ts = float(raw_ts)
            except (ValueError, TypeError):
                raw_ts = 0
        ts = raw_ts / 1000 if raw_ts > 1e12 else raw_ts
        if ts:
            age_s = now - ts
            if age_s < 3600:
                age = f"{int(age_s / 60)}m ago"
            elif age_s < 86400:
                age = f"{int(age_s / 3600)}h ago"
            else:
                age = f"{int(age_s / 86400)}d ago"
        else:
            age = "?"

        prompt = r.first_prompt.replace("\n", " ").strip()
        if len(prompt) > 80:
            prompt = prompt[:77] + "..."

        status_label = "done" if r.status == "finished" else r.status

        typer.echo(f"\n  {age:<8} [{status_label}] {r.project}")
        if prompt:
            typer.echo(f"           {prompt}")
        if r.status != "finished":
            if r.cwd:
                typer.echo(f"           cd {r.cwd} && claude --resume {r.session_id}")
            else:
                typer.echo(f"           claude --resume {r.session_id}")

    typer.echo("")


@app.command()
def overlays() -> None:
    """List overlays (from ~/.teatree.toml and installed entry points)."""
    from teetree.config import discover_active_overlay, discover_overlays  # noqa: PLC0415

    installed = discover_overlays()
    active = discover_active_overlay()

    if not installed:
        typer.echo("No overlays found.")
        typer.echo("Add one to ~/.teatree.toml:")
        typer.echo("")
        typer.echo("  [overlays.my-project]")
        typer.echo('  path = "~/workspace/my-project"')
        return

    typer.echo("Installed overlays:")
    for entry in installed:
        marker = " (active)" if active and entry.name == active.name else ""
        typer.echo(f"  {entry.name:<20}{entry.settings_module}{marker}")


# ── Top-level info ─────────────────────────────────────────────────────


@app.command()
def info() -> None:
    """Show t3 installation, teatree/overlay sources, and editable status."""
    _show_info()


config_app = typer.Typer(no_args_is_help=True, help="Configuration and autoloading.")
app.add_typer(config_app, name="config")


@config_app.command(name="write-skill-cache")
def write_skill_cache() -> None:
    """Write overlay skill metadata to XDG cache for hook consumption."""
    import json as _json  # noqa: PLC0415

    import django  # noqa: PLC0415

    from teetree.config import DATA_DIR, discover_active_overlay  # noqa: PLC0415

    active = discover_active_overlay()
    if active and "DJANGO_SETTINGS_MODULE" not in os.environ:
        os.environ["DJANGO_SETTINGS_MODULE"] = active.settings_module
    django.setup()

    from teetree.core.overlay_loader import get_overlay  # noqa: PLC0415

    overlay = get_overlay()
    metadata = overlay.get_skill_metadata()
    cache_path = DATA_DIR / "skill-metadata.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(_json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"Wrote skill metadata to {cache_path}")


@config_app.command()
def autoload() -> None:
    """List skill auto-loading rules from context-match.yml files."""
    from teetree.agents.skill_bundle import DEFAULT_SKILLS_DIR  # noqa: PLC0415

    skills_dir = DEFAULT_SKILLS_DIR
    if not skills_dir.is_dir():
        typer.echo(f"Skills directory not found: {skills_dir}")
        raise typer.Exit(code=1)

    found = False
    for skill in sorted(skills_dir.iterdir()):
        match_file = skill / "hook-config" / "context-match.yml"
        if not match_file.is_file():
            continue
        found = True
        typer.echo(f"\n{skill.name}:")
        typer.echo(match_file.read_text(encoding="utf-8").rstrip())

    if not found:
        typer.echo("No context-match.yml files found in any skill directory.")


@config_app.command()
def cache() -> None:
    """Show the XDG skill-metadata cache content."""
    import json as _json  # noqa: PLC0415

    from teetree.config import DATA_DIR  # noqa: PLC0415

    cache_path = DATA_DIR / "skill-metadata.json"
    if not cache_path.is_file():
        typer.echo(f"No cache found at {cache_path}")
        typer.echo("Run: uv run t3 config write-skill-cache")
        raise typer.Exit(code=1)

    data = _json.loads(cache_path.read_text(encoding="utf-8"))
    typer.echo(f"Cache: {cache_path}")
    typer.echo(_json.dumps(data, indent=2))


def _find_overlay_project() -> Path:
    """Find the active overlay project root."""
    from teetree.config import discover_active_overlay  # noqa: PLC0415

    active = discover_active_overlay()
    if active and active.project_path:
        return active.project_path
    return _find_project_root()


# dashboard and resetdb are registered per-overlay in _register_overlay_commands()


def _find_project_root() -> Path:
    """Walk up from cwd to find the project root (contains pyproject.toml)."""
    for directory in [Path.cwd(), *Path.cwd().parents]:
        if (directory / "pyproject.toml").is_file():
            return directory
    return Path.cwd()


# ── Non-Django command groups ──────────────────────────────────────────

ci_app = typer.Typer(no_args_is_help=True, help="CI pipeline helpers.")
app.add_typer(ci_app, name="ci")

review_app = typer.Typer(no_args_is_help=True, help="Code review helpers.")
app.add_typer(review_app, name="review")

review_request_app = typer.Typer(no_args_is_help=True, help="Batch review requests.")
app.add_typer(review_request_app, name="review-request")

doctor_app = typer.Typer(no_args_is_help=True, help="Smoke-test hooks, imports, services.")
app.add_typer(doctor_app, name="doctor")

_REQUIRED_TOOLS = ("direnv", "git", "jq")

tool_app = typer.Typer(no_args_is_help=True, help="Standalone utilities.")
app.add_typer(tool_app, name="tool")


def _scripts_dir() -> Path:
    """Locate the scripts/ directory relative to the teatree package."""
    return Path(__file__).resolve().parent.parent.parent / "scripts"


def _run_script(script_name: str, *args: str) -> None:
    """Run a script from the scripts/ directory."""
    scripts = _scripts_dir()
    script = scripts / f"{script_name}.py"
    if not script.is_file():
        typer.echo(f"Script not found: {script}")
        raise typer.Exit(code=1)
    cmd = [sys.executable, str(script), *args]
    result = subprocess.run(cmd, check=False)  # noqa: S603
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)


# ── Review commands ───────────────────────────────────────────────────


@review_app.command(name="post-draft-note")
def post_draft_note(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
    note: str = typer.Argument(help="Comment text (markdown)"),
    file: str = typer.Option("", help="File path for inline comment (omit for general note)"),
    line: int = typer.Option(0, help="Line number in the new file (must be an added line)"),
) -> None:
    """Post a draft note on a GitLab MR (inline or general).

    Gets the GitLab token from glab auth automatically. For inline notes,
    fetches diff refs and validates the target line is an added line.
    """
    from teetree.utils.gitlab_api import GitLabAPI  # noqa: PLC0415

    token = _get_gitlab_token()
    if not token:
        typer.echo("No GitLab token found. Run: glab auth login")
        raise typer.Exit(code=1)

    api = GitLabAPI(token=token)
    encoded = repo.replace("/", "%2F")

    if file and line:
        # Inline note — get diff refs first
        mr_data = api.get_json(f"projects/{encoded}/merge_requests/{mr}")
        if not isinstance(mr_data, dict):
            typer.echo(f"Could not fetch MR !{mr} from {repo}")
            raise typer.Exit(code=1)

        diff_refs_raw = mr_data.get("diff_refs", {})
        if not isinstance(diff_refs_raw, dict):
            typer.echo("MR has no diff_refs")
            raise typer.Exit(code=1)
        diff_refs: dict[str, str] = {str(k): str(v) for k, v in diff_refs_raw.items()}

        payload: dict[str, object] = {
            "note": note,
            "position": {
                "position_type": "text",
                "base_sha": diff_refs["base_sha"],
                "head_sha": diff_refs["head_sha"],
                "start_sha": diff_refs["start_sha"],
                "old_path": file,
                "new_path": file,
                "new_line": line,
            },
        }
    else:
        # General note
        payload = {"note": note}

    result = api.post_json(f"projects/{encoded}/merge_requests/{mr}/draft_notes", payload)
    if not result:
        typer.echo("Failed to post draft note")
        raise typer.Exit(code=1)

    result_dict = dict(result) if isinstance(result, dict) else {}
    note_id = result_dict.get("id")
    position_raw = result_dict.get("position")
    position = dict(position_raw) if isinstance(position_raw, dict) else {}
    line_code = position.get("line_code")

    if file and line and not line_code:
        typer.echo(f"WARNING: line_code is null — note may not render inline (line {line} in {file}).")

    typer.echo(f"OK draft_note_id={note_id}" + (f" line_code={line_code}" if line_code else ""))


@review_app.command(name="delete-draft-note")
def delete_draft_note(
    repo: str = typer.Argument(help="GitLab project path"),
    mr: int = typer.Argument(help="Merge request IID"),
    note_id: int = typer.Argument(help="Draft note ID to delete"),
) -> None:
    """Delete a draft note from a GitLab MR."""
    import httpx as _httpx  # noqa: PLC0415

    token = _get_gitlab_token()
    if not token:
        typer.echo("No GitLab token found.")
        raise typer.Exit(code=1)

    encoded = repo.replace("/", "%2F")
    response = _httpx.delete(
        f"https://gitlab.com/api/v4/projects/{encoded}/merge_requests/{mr}/draft_notes/{note_id}",
        headers={"PRIVATE-TOKEN": token},
        timeout=10.0,
    )
    if response.status_code == 204:
        typer.echo(f"OK deleted draft_note_id={note_id}")
    else:
        typer.echo(f"Failed: HTTP {response.status_code}")
        raise typer.Exit(code=1)


@review_app.command(name="list-draft-notes")
def list_draft_notes(
    repo: str = typer.Argument(help="GitLab project path"),
    mr: int = typer.Argument(help="Merge request IID"),
) -> None:
    """List draft notes on a GitLab MR."""
    from teetree.utils.gitlab_api import GitLabAPI  # noqa: PLC0415

    token = _get_gitlab_token()
    if not token:
        typer.echo("No GitLab token found.")
        raise typer.Exit(code=1)

    api = GitLabAPI(token=token)
    encoded = repo.replace("/", "%2F")
    notes = api.get_json(f"projects/{encoded}/merge_requests/{mr}/draft_notes")
    if not isinstance(notes, list):
        typer.echo("No draft notes found")
        return

    for n in notes:
        if not isinstance(n, dict):
            continue
        entry: dict[str, object] = n
        nid = entry.get("id")
        pos_raw = entry.get("position")
        pos = dict(pos_raw) if isinstance(pos_raw, dict) else {}
        fp = pos.get("new_path", "")
        ln = pos.get("new_line", "")
        body = str(entry.get("note", ""))[:60]
        typer.echo(f"  {nid}  {fp}:{ln}  {body}...")


def _get_gitlab_token() -> str:
    """Extract GitLab token from glab auth or GITLAB_TOKEN env var."""
    import os  # noqa: PLC0415

    token = os.environ.get("GITLAB_TOKEN", "") or os.environ.get("TEATREE_GITLAB_TOKEN", "")
    if token:
        return token
    result = subprocess.run(
        ["glab", "auth", "status", "-t"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stderr.splitlines():
        if "Token:" in line:
            parts = line.split(":", 1)
            if len(parts) == 2:  # pragma: no branch
                return parts[1].strip()
    return ""


# ── CI commands ──────────────────────────────────────────────────────


@ci_app.command()
def cancel(
    branch: str = typer.Argument("", help="Branch name (default: current branch)"),
) -> None:
    """Cancel stale CI pipelines for a branch."""
    ci = _get_ci_service()
    if ci is None:
        typer.echo("No CI service configured (set TEATREE_GITLAB_TOKEN).")
        raise typer.Exit(code=1)

    project = _get_ci_project()
    ref = branch or _current_git_branch()
    if not ref:
        typer.echo("Could not detect branch. Pass one explicitly.")
        raise typer.Exit(code=1)

    cancelled = ci.cancel_pipelines(project=project, ref=ref)
    if cancelled:
        typer.echo(f"Cancelled {len(cancelled)} pipeline(s): {cancelled}")
    else:
        typer.echo("No running/pending pipelines found.")


@ci_app.command()
def divergence() -> None:
    """Check fork divergence from upstream."""
    from teetree.utils.git import run as git_run  # noqa: PLC0415

    try:
        git_run(repo=".", args=["fetch", "upstream"])
    except Exception:  # noqa: BLE001
        typer.echo("No upstream remote configured. Add one: git remote add upstream <url>")
        raise typer.Exit(code=1) from None

    from teetree.utils.git import current_branch  # noqa: PLC0415

    branch = current_branch()
    ahead = git_run(repo=".", args=["rev-list", "--count", f"upstream/{branch}..HEAD"]).strip()
    behind = git_run(repo=".", args=["rev-list", "--count", f"HEAD..upstream/{branch}"]).strip()
    typer.echo(f"Branch {branch}: {ahead} ahead, {behind} behind upstream")


@ci_app.command(name="fetch-errors")
def fetch_errors(
    branch: str = typer.Argument("", help="Branch name (default: current branch)"),
) -> None:
    """Fetch error logs from the latest CI pipeline."""
    ci = _get_ci_service()
    if ci is None:
        typer.echo("No CI service configured (set TEATREE_GITLAB_TOKEN).")
        raise typer.Exit(code=1)

    project = _get_ci_project()
    ref = branch or _current_git_branch()
    errors = ci.fetch_pipeline_errors(project=project, ref=ref)
    if errors:
        for error in errors:
            typer.echo(error)
            typer.echo("---")
    else:
        typer.echo("No errors found in the latest pipeline.")


@ci_app.command(name="fetch-failed-tests")
def fetch_failed_tests(
    branch: str = typer.Argument("", help="Branch name (default: current branch)"),
) -> None:
    """Extract failed test IDs from the latest CI pipeline."""
    ci = _get_ci_service()
    if ci is None:
        typer.echo("No CI service configured (set TEATREE_GITLAB_TOKEN).")
        raise typer.Exit(code=1)

    project = _get_ci_project()
    ref = branch or _current_git_branch()
    failed = ci.fetch_failed_tests(project=project, ref=ref)
    if failed:
        typer.echo(f"Failed tests ({len(failed)}):")
        for test_id in failed:
            typer.echo(f"  {test_id}")
    else:
        typer.echo("No failed tests found.")


@ci_app.command(name="trigger-e2e")
def trigger_e2e(
    branch: str = typer.Argument("", help="Branch name (default: current branch)"),
) -> None:
    """Trigger E2E tests on CI."""
    ci = _get_ci_service()
    if ci is None:
        typer.echo("No CI service configured (set TEATREE_GITLAB_TOKEN).")
        raise typer.Exit(code=1)

    project = _get_ci_project()
    ref = branch or _current_git_branch()
    result = ci.trigger_pipeline(project=project, ref=ref, variables={"E2E": "true"})
    if "error" in result:
        typer.echo(f"Error: {result['error']}")
        raise typer.Exit(code=1)
    typer.echo(f"Pipeline triggered: {result.get('web_url', result.get('id', 'unknown'))}")


@ci_app.command(name="quality-check")
def quality_check(
    branch: str = typer.Argument("", help="Branch name (default: current branch)"),
) -> None:
    """Run quality analysis (fetch test report from latest pipeline)."""
    ci = _get_ci_service()
    if ci is None:
        typer.echo("No CI service configured (set TEATREE_GITLAB_TOKEN).")
        raise typer.Exit(code=1)

    project = _get_ci_project()
    ref = branch or _current_git_branch()
    report = ci.quality_check(project=project, ref=ref)
    if "error" in report:
        typer.echo(f"Error: {report['error']}")
        raise typer.Exit(code=1)
    typer.echo(f"Pipeline {report.get('pipeline_id')}: {report.get('status')}")
    typer.echo(f"  Total: {report.get('total_count', 0)}")
    typer.echo(f"  Passed: {report.get('success_count', 0)}")
    typer.echo(f"  Failed: {report.get('failed_count', 0)}")


def _get_ci_service():  # noqa: ANN202
    """Get CI service — tries Django settings first, falls back to env vars."""
    try:
        from teetree.backends.loader import get_ci_service  # noqa: PLC0415

        return get_ci_service()
    except Exception:  # noqa: BLE001, S110 — fallback to env-based config
        pass

    token = os.environ.get("TEATREE_GITLAB_TOKEN", os.environ.get("GITLAB_TOKEN", ""))
    if token:
        from teetree.backends.gitlab_ci import GitLabCIService  # noqa: PLC0415
        from teetree.utils.gitlab_api import GitLabAPI  # noqa: PLC0415

        base_url = os.environ.get("TEATREE_GITLAB_URL", "https://gitlab.com/api/v4")
        return GitLabCIService(client=GitLabAPI(token=token, base_url=base_url))
    return None


def _get_ci_project() -> str:
    """Get the CI project path — from overlay or git remote."""
    try:
        import django  # noqa: PLC0415

        django.setup()
        from teetree.core.overlay_loader import get_overlay  # noqa: PLC0415

        project = get_overlay().get_ci_project_path()
        if project:
            return project
    except Exception:  # noqa: BLE001, S110 — Django may not be configured
        pass

    from teetree.utils.gitlab_api import GitLabAPI  # noqa: PLC0415

    project_info = GitLabAPI().resolve_project_from_remote()
    return project_info.path_with_namespace if project_info else ""


def _current_git_branch() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


# ── Doctor commands ──────────────────────────────────────────────────


def _show_info() -> None:
    """Display t3 entry point, teatree/overlay sources, and editable status."""
    import shutil  # noqa: PLC0415

    from teetree.config import discover_active_overlay, discover_overlays  # noqa: PLC0415

    t3_bin = shutil.which("t3") or "not found on PATH"
    teatree_editable, _teatree_url = _editable_info("teatree")
    editable_label = " (editable)" if teatree_editable else ""
    typer.echo(f"t3 entry point:   {t3_bin}{editable_label}")
    typer.echo(f"Python:           {sys.executable}")
    typer.echo()

    _print_package_info("teatree", "teetree")

    active = discover_active_overlay()
    if active:
        typer.echo(f"Active overlay:   {active.name} ({active.settings_module})")
    else:
        typer.echo("Active overlay:   (none)")

    installed = discover_overlays()
    if installed:
        typer.echo()
        typer.echo("Installed overlays:")
        for entry in installed:
            typer.echo(f"  {entry.name:<20}{entry.settings_module}")


def _collect_overlay_skills() -> list[tuple[Path, str]]:
    """Discover skill directories from registered overlay projects.

    Returns (target_dir, link_name) pairs for symlink creation.
    """
    from teetree.config import discover_overlays  # noqa: PLC0415

    results: list[tuple[Path, str]] = []
    for entry in discover_overlays():
        if not entry.project_path or not entry.project_path.is_dir():
            continue
        project = entry.project_path.expanduser()
        overlay_name = entry.name if entry.name.startswith("t3-") else f"t3-{entry.name}"

        # New convention: skills/ directory
        project_skills = project / "skills"
        if project_skills.is_dir():
            results.extend(
                (skill, skill.name) for skill in sorted(project_skills.iterdir()) if (skill / "SKILL.md").is_file()
            )

        # Legacy convention: overlay app dir with SKILL.md
        for subdir in sorted(project.iterdir()):
            if subdir.is_dir() and subdir.name != "skills" and (subdir / "SKILL.md").is_file():
                results.append((subdir, overlay_name))
                break  # one overlay skill per project
    return results


def _repair_symlinks(skills_dir: Path, claude_skills: Path) -> tuple[int, int]:
    """Create or fix symlinks for core and overlay skills. Returns (created, fixed)."""
    created = 0
    fixed = 0

    def _ensure(target: Path, link: Path) -> None:
        nonlocal created, fixed
        if link.is_symlink():
            if link.resolve() == target.resolve():
                return
            link.unlink()
            fixed += 1
        elif link.exists():
            return  # real directory, don't touch
        link.symlink_to(target)
        created += 1

    for skill in sorted(skills_dir.iterdir()):  # pragma: no branch
        if (skill / "SKILL.md").is_file():
            _ensure(skill, claude_skills / skill.name)

    for target, link_name in _collect_overlay_skills():
        _ensure(target, claude_skills / link_name)

    return created, fixed


@doctor_app.command()
def repair() -> None:
    """Repair skill symlinks and verify installation health."""
    from teetree.agents.skill_bundle import DEFAULT_SKILLS_DIR  # noqa: PLC0415

    skills_dir = DEFAULT_SKILLS_DIR
    if not skills_dir.is_dir():
        typer.echo(f"Skills directory not found: {skills_dir}")
        raise typer.Exit(code=1)

    claude_skills = Path.home() / ".claude" / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)

    created, fixed = _repair_symlinks(skills_dir, claude_skills)

    # Clean broken symlinks
    removed = 0
    for link in claude_skills.iterdir():
        if link.is_symlink() and not link.exists():
            link.unlink()
            removed += 1

    typer.echo(f"Skills: {created} created, {fixed} fixed, {removed} broken removed")
    typer.echo(f"Source: {skills_dir}")
    overlay_skills = _collect_overlay_skills()
    if overlay_skills:
        typer.echo(f"Overlays: {len(overlay_skills)} overlay skill(s) managed")


@doctor_app.command()
def check() -> bool:
    """Verify imports, required tools, and editable-install sanity."""
    ok = True

    try:
        import django  # noqa: PLC0415, F401

        import teetree.core  # noqa: PLC0415, F401
    except ImportError as exc:
        typer.echo(f"FAIL  Import check: {exc}")
        return False

    for tool in _REQUIRED_TOOLS:
        if not shutil.which(tool):
            typer.echo(f"FAIL  Required tool not found: {tool}")
            ok = False

    for problem in _check_editable_sanity():
        typer.echo(f"WARN  {problem}")
        ok = False

    if ok:
        typer.echo("All checks passed")
    return ok


def _check_editable_sanity() -> list[str]:
    """Verify editable status matches declared intent.

    Settings can declare:
        TEATREE_EDITABLE = True   # contributing to teatree
        OVERLAY_EDITABLE = True   # contributing to overlay

    If not set, defaults to False (normal install expected).
    """
    problems: list[str] = []

    try:
        if "DJANGO_SETTINGS_MODULE" not in os.environ:
            from teetree.config import discover_active_overlay  # noqa: PLC0415

            active = discover_active_overlay()
            if active:
                os.environ["DJANGO_SETTINGS_MODULE"] = active.settings_module
            else:
                return problems  # no overlay, no settings to check

        import django  # noqa: PLC0415

        django.setup()
        from django.conf import settings as django_settings  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — Django may not be installed
        return problems

    # Check teatree
    teatree_should_be_editable = getattr(django_settings, "TEATREE_EDITABLE", False)
    teatree_is_editable, _ = _editable_info("teatree")

    if teatree_should_be_editable and not teatree_is_editable:
        problems.append(
            "TEATREE_EDITABLE=True but teatree is not editable. "
            "Changes to teatree source will be lost. "
            "Fix: add `teatree = { path = '...', editable = true }` to [tool.uv.sources]"
        )
    elif not teatree_should_be_editable and teatree_is_editable:
        problems.append(
            "teatree is editable but TEATREE_EDITABLE is not set. "
            "You risk accidentally modifying framework code. "
            "Fix: set TEATREE_EDITABLE = True in settings.py if contributing, "
            "or remove the editable source."
        )

    # Check overlay
    overlay_class = getattr(django_settings, "TEATREE_OVERLAY_CLASS", "")
    if overlay_class:
        top_package = overlay_class.rsplit(".", 1)[0].split(".")[0]
        from importlib.metadata import packages_distributions  # noqa: PLC0415

        dist_map = packages_distributions()
        dist_names = dist_map.get(top_package, [top_package])
        overlay_dist = dist_names[0] if dist_names else top_package

        overlay_should_be_editable = getattr(django_settings, "OVERLAY_EDITABLE", False)
        overlay_is_editable, _ = _editable_info(overlay_dist)

        if overlay_should_be_editable and not overlay_is_editable:
            problems.append(
                "OVERLAY_EDITABLE=True but overlay is not editable. "
                "Agent changes to overlay code will be lost. "
                "Fix: run `uv pip install -e .`"
            )
        elif not overlay_should_be_editable and overlay_is_editable:
            problems.append(
                f"Overlay ({overlay_dist}) is editable but OVERLAY_EDITABLE is not set. "
                "Fix: set OVERLAY_EDITABLE = True in settings.py if contributing."
            )

    return problems


@doctor_app.command(name="info")
def doctor_info() -> None:
    """Show t3 path, teatree/overlay sources, and editable status."""
    _show_info()


# ── Review-request commands ──────────────────────────────────────────


@review_request_app.command()
def discover() -> None:
    """Discover open merge requests awaiting review."""
    project = _find_overlay_project()
    _managepy(project, "followup", "discover-mrs")


# ── Tool commands ────────────────────────────────────────────────────


@tool_app.command("privacy-scan")
def privacy_scan(
    path: str = typer.Argument("-", help="File or '-' for stdin"),
) -> None:
    """Scan text for privacy-sensitive patterns (emails, keys, IPs)."""
    _run_script("privacy_scan", path)


@tool_app.command("analyze-video")
def analyze_video(
    video_path: str = typer.Argument(..., help="Path to video file"),
) -> None:
    """Decompose video into frames for AI analysis."""
    _run_script("analyze_video", video_path)


@tool_app.command("bump-deps")
def bump_deps() -> None:
    """Bump pyproject.toml dependencies from uv.lock."""
    _run_script("bump-pyproject-deps-from-lock-file")


@tool_app.command("sonar-check")
def sonar_check(
    repo_path: str = typer.Argument("", help="Path to repo (default: current directory)"),
    *,
    skip_baseline: bool = typer.Option(default=False, help="Reuse previous baseline"),
    remote: bool = typer.Option(default=False, help="Push to CI server instead of local"),
    remote_status: bool = typer.Option(default=False, help="Fetch CI Sonar results"),
) -> None:
    """Run local SonarQube analysis via Docker."""
    project = _find_overlay_project()
    script = project / "scripts" / "sonar_check.sh"
    if not script.is_file():
        typer.echo(f"sonar_check.sh not found in {project / 'scripts'}")
        raise typer.Exit(code=1)
    cmd = ["bash", str(script)]
    if not repo_path:
        # Recover the user's original shell CWD — os.getcwd() may differ when
        # invoked via ``uv --directory`` which calls os.chdir() but leaves the
        # inherited $PWD env var untouched.
        repo_path = os.environ.get("PWD", str(Path.cwd()))
    cmd.append(repo_path)
    if skip_baseline:
        cmd.append("--skip-baseline")
    if remote:
        cmd.append("--remote")
    if remote_status:
        cmd.append("--remote-status")
    subprocess.run(cmd, check=True)  # noqa: S603


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
    from teetree.agents.handover import build_claude_handover_status  # noqa: PLC0415

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


# ── Startproject helpers ─────────────────────────────────────────────


def _patch_settings(settings_path: Path, overlay_app: str, overlay_class_name: str) -> None:
    text = settings_path.read_text(encoding="utf-8")
    extra_apps = [
        "django_htmx",
        "django_rich",
        "django_tasks",
        "django_tasks_db",
        "teetree.core",
        "teetree.agents",
        overlay_app,
    ]
    teatree_apps = "\n".join(f"    '{a}'," for a in extra_apps)
    text = text.replace(
        "'django.contrib.staticfiles',",
        f"'django.contrib.staticfiles',\n{teatree_apps}",
    )
    text += dedent(f"""

        # --- TeaTree ---
        TEATREE_OVERLAY_CLASS = "{overlay_app}.overlay.{overlay_class_name}Overlay"
        TEATREE_HEADLESS_RUNTIME = "claude-code"
        TEATREE_INTERACTIVE_RUNTIME = "codex"
        TEATREE_TERMINAL_MODE = "same-terminal"
        TEATREE_CLAUDE_STATUSLINE_STATE_DIR = "/tmp/claude-statusline"
        TEATREE_AGENT_HANDOVER = [
            {{
                "runtime": "claude-code",
                "telemetry": {{
                    "provider": "claude-statusline",
                    "switch_away_at_percent": 95,
                    "switch_back_at_percent": 80,
                }},
            }},
            {{
                "runtime": "codex",
            }},
        ]

        TASKS = {{
            "default": {{
                "BACKEND": "django_tasks_db.DatabaseBackend",
            }},
        }}

        # Editable-install intent (verified by `t3 doctor check`).
        # Set to True when contributing to that package's source code.
        TEATREE_EDITABLE = False
        OVERLAY_EDITABLE = False
    """)
    settings_path.write_text(text, encoding="utf-8")


def _patch_urls(urls_path: Path) -> None:
    text = urls_path.read_text(encoding="utf-8")
    text = text.replace(
        "from django.urls import path",
        "from django.urls import include, path",
    )
    text = text.replace(
        "path('admin/', admin.site.urls),",
        "path('', include('teetree.core.urls')),\n    path('admin/', admin.site.urls),",
    )
    urls_path.write_text(text, encoding="utf-8")


def _patch_manage_py(manage_py: Path) -> None:
    text = manage_py.read_text(encoding="utf-8")
    text = text.replace(
        "import sys\n",
        "import sys\nfrom pathlib import Path\n",
    )
    text = text.replace(
        "os.environ.setdefault",
        'sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))\n    os.environ.setdefault',
    )
    manage_py.write_text(text, encoding="utf-8")


def _write_overlay(overlay_path: Path, overlay_app: str, overlay_class_name: str, skill_name: str) -> None:  # noqa: ARG001
    overlay_path.write_text(
        dedent(f"""\
            from teetree.core.overlay import OverlayBase, ProvisionStep


            class {overlay_class_name}Overlay(OverlayBase):
                def get_repos(self) -> list[str]:
                    return []

                def get_provision_steps(self, worktree):
                    return []

                def get_skill_metadata(self):
                    return {{"skill_path": "skills/{skill_name}/SKILL.md"}}
        """),
        encoding="utf-8",
    )


def _write_skill_md(skill_path: Path, project_name: str, skill_name: str) -> None:
    skill_path.write_text(
        dedent(f"""\
            ---
            name: {skill_name}
            description: Project overlay skill for {project_name}.
            requires:
                - t3-workspace
            metadata:
                version: 0.0.1
            ---

            # {skill_name}

            Project overlay skill companion for {project_name}.
        """),
        encoding="utf-8",
    )


def _copy_config_templates(project_root: Path) -> None:
    from importlib.resources import files  # noqa: PLC0415

    template_dir = files("teetree.templates").joinpath("overlay")
    # Map source filename -> destination filename.
    # .pre-commit-config.yaml is stored as .tmpl to prevent prek from
    # discovering the template directory as a sub-project.
    templates = {
        ".editorconfig": ".editorconfig",
        ".gitignore": ".gitignore",
        ".markdownlint-cli2.yaml": ".markdownlint-cli2.yaml",
        ".pre-commit-config.yaml.tmpl": ".pre-commit-config.yaml",
        ".python-version": ".python-version",
    }
    for source_name, dest_name in templates.items():
        source = template_dir.joinpath(source_name)
        dest = project_root / dest_name
        dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _write_pyproject(project_root: Path, project_name: str, overlay_app: str, package_name: str) -> None:
    from importlib.resources import files  # noqa: PLC0415

    template = (
        files("teetree.templates").joinpath("overlay").joinpath("pyproject.toml.tmpl").read_text(encoding="utf-8")
    )
    content = template.replace("{{project_name}}", project_name)
    content = content.replace("{{overlay_app}}", overlay_app)
    content = content.replace("{{package_name}}", package_name)
    content = content.replace("{{description}}", f"Generated TeaTree host project for {_camelize(overlay_app)}")
    project_root.joinpath("pyproject.toml").write_text(content, encoding="utf-8")


def _camelize(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


# ── Django bootstrap ──────────────────────────────────────────────────


# ── Django-dependent command groups ───────────────────────────────────

_DJANGO_GROUPS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "lifecycle": (
        "Worktree lifecycle.",
        [
            ("setup", "Create and provision a worktree."),
            ("start", "Start services for a worktree."),
            ("status", "Return worktree state information."),
            ("teardown", "Tear down a worktree."),
            ("clean", "Teardown worktree — stop services, drop DB, clean state."),
            ("diagram", "Print the lifecycle state diagram as Mermaid."),
        ],
    ),
    "workspace": (
        "Workspace management.",
        [
            ("ticket", "Create a ticket with worktree entries for each repo."),
            ("finalize", "Squash worktree commits and rebase on the default branch."),
            ("clean-all", "Prune worktrees whose branches have been merged or deleted."),
        ],
    ),
    "run": (
        "Run services.",
        [
            ("verify", "Verify worktree state and return URLs."),
            ("services", "Return configured run commands."),
            ("backend", "Start the backend dev server."),
            ("frontend", "Start the frontend dev server."),
            ("build-frontend", "Build the frontend for production/testing."),
            ("tests", "Run the project test suite."),
            ("e2e", "Run E2E tests via CI or overlay config."),
        ],
    ),
    "db": (
        "Database operations.",
        [
            ("refresh", "Re-import the worktree database from dump/DSLR."),
            ("restore-ci", "Restore database from the latest CI dump."),
            ("reset-passwords", "Reset all user passwords to a known dev value."),
        ],
    ),
    "pr": (
        "Pull request helpers.",
        [
            ("create", "Create a merge request for the ticket's branch."),
            ("check-gates", "Check whether session gates allow a phase transition."),
            ("fetch-issue", "Fetch issue details from the configured tracker."),
            ("detect-tenant", "Detect the current tenant variant from the overlay."),
            ("post-evidence", "Post test evidence as an MR comment."),
        ],
    ),
    "tasks": (
        "Async task queue.",
        [
            ("claim", "Claim the next available task."),
            ("work-next-sdk", "Claim and execute an headless task."),
            ("work-next-user-input", "Claim and execute a user input task."),
        ],
    ),
    "followup": (
        "Follow-up snapshots.",
        [
            ("refresh", "Return counts of tickets and tasks."),
            ("sync", "Synchronize followup data from MRs."),
            ("remind", "Return list of pending user input tasks."),
        ],
    ),
}


def _register_overlay_commands() -> None:
    """Register all installed overlays as subcommand groups.

    No Django bootstrap needed — commands delegate to manage.py via subprocess.
    """
    from teetree.config import discover_active_overlay, discover_overlays  # noqa: PLC0415

    active = discover_active_overlay()
    installed = discover_overlays()

    # For each installed overlay, register a subcommand group
    for entry in installed:
        short_name = entry.settings_module.split(".")[0]
        project_path = entry.project_path or (active.project_path if active and active.name == entry.name else None)
        overlay_app = _build_overlay_app(entry.name, project_path, entry.settings_module)
        app.add_typer(overlay_app, name=short_name)


def _managepy(project_path: Path | None, *args: str) -> None:
    """Run manage.py in the overlay project directory."""
    if project_path is None:
        typer.echo("Cannot find overlay project directory (no manage.py in cwd ancestors).")
        raise typer.Exit(code=1)

    manage_py = project_path / "manage.py"
    if not manage_py.is_file():
        typer.echo(f"No manage.py found in {project_path}")
        raise typer.Exit(code=1)

    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    subprocess.run(  # noqa: S603
        [sys.executable, str(manage_py), *args],
        cwd=project_path,
        env=env,
        check=True,
    )


def _uvicorn(project_path: Path | None, host: str, port: int, settings_module: str = "") -> None:
    """Start uvicorn for the overlay project's ASGI application."""
    if project_path is None:
        typer.echo("Cannot find overlay project directory.")
        raise typer.Exit(code=1)

    settings_module = settings_module or os.environ.get("DJANGO_SETTINGS_MODULE", "")
    asgi_module = settings_module.rsplit(".", 1)[0] + ".asgi:application" if settings_module else "asgi:application"

    # Use the project's venv Python so uvicorn (a project dependency) is importable.
    project_python = project_path / ".venv" / "bin" / "python"
    python = str(project_python) if project_python.is_file() else sys.executable

    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    subprocess.run(  # noqa: S603
        [python, "-m", "uvicorn", asgi_module, "--host", host, "--port", str(port), "--reload"],
        cwd=project_path,
        env=env,
        check=False,
    )


def _build_overlay_app(overlay_name: str, project_path: Path | None, settings_module: str = "") -> typer.Typer:
    """Build a Typer app with overlay commands that delegate to manage.py."""
    overlay_app = typer.Typer(no_args_is_help=True, help=f"Commands for the {overlay_name} overlay.")

    @overlay_app.command()
    def dashboard(
        host: str = typer.Option("127.0.0.1", help="Host to bind to"),
        port: int = typer.Option(8000, help="Port to serve on"),
    ) -> None:
        """Migrate the database and start the dashboard dev server."""
        import socket  # noqa: PLC0415

        _managepy(project_path, "migrate", "--no-input")
        actual_port = port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) == 0:
                # Port in use — find a free one
                s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s2.bind((host, 0))
                actual_port = s2.getsockname()[1]
                s2.close()
                typer.echo(f"Port {port} in use, using {actual_port}")
        _uvicorn(project_path, host, actual_port, settings_module)

    @overlay_app.command()
    def resetdb() -> None:
        """Drop the SQLite database and re-run all migrations."""
        from teetree.config import get_data_dir  # noqa: PLC0415

        db_path = get_data_dir(overlay_name) / "db.sqlite3"
        if db_path.exists():
            db_path.unlink()
            typer.echo(f"Deleted {db_path}")
        _managepy(project_path, "migrate", "--no-input")
        typer.echo("Database recreated.")

    @overlay_app.command()
    def worker(
        count: int = typer.Option(3, help="Number of worker processes"),
        interval: float = typer.Option(1.0, help="Polling interval in seconds"),
    ) -> None:
        """Start background task workers."""
        if project_path is None:
            typer.echo("Cannot find overlay project directory.")
            raise typer.Exit(code=1)

        manage_py = str(project_path / "manage.py")
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        processes = []
        for _ in range(count):
            p = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    manage_py,
                    "db_worker",
                    "--interval",
                    str(interval),
                    "--no-startup-delay",
                    "--no-reload",
                ],
                cwd=project_path,
                env=env,
            )
            processes.append(p)

        typer.echo(f"Started {count} worker(s). Press Ctrl+C to stop.")
        try:
            for p in processes:
                p.wait()
        except KeyboardInterrupt:
            typer.echo("Shutting down workers...")
            for p in processes:  # pragma: no branch
                p.terminate()
            for p in processes:  # pragma: no branch
                p.wait(timeout=5)

    # ── Overlay-scoped shortcuts ──────────────────────────────────────

    @overlay_app.command(name="full-status")
    def full_status() -> None:
        """Show ticket, worktree, and session state summary."""
        _managepy(project_path, "followup", "refresh")

    @overlay_app.command(name="start-ticket")
    def start_ticket(
        issue_url: str = typer.Argument(help="Issue/ticket URL"),
        variant: str = typer.Option("", help="Tenant variant"),
    ) -> None:
        """Zero to coding — create ticket, provision worktree, start services."""
        args = ["workspace", "ticket", issue_url]
        if variant:
            args.extend(["--variant", variant])
        _managepy(project_path, *args)

    @overlay_app.command(name="ship")
    def ship(
        ticket_id: str = typer.Argument(help="Ticket ID"),
        title: str = typer.Option("", help="MR title"),
    ) -> None:
        """Code to MR — create merge request for the ticket."""
        args = ["pr", "create", ticket_id]
        if title:
            args.extend(["--title", title])
        _managepy(project_path, *args)

    @overlay_app.command(name="daily")
    def daily() -> None:
        """Daily followup — sync MRs, check gates, remind reviewers."""
        _managepy(project_path, "followup", "sync")

    @overlay_app.command(name="agent")
    def overlay_agent(
        task: str = typer.Argument("", help="What to work on"),
    ) -> None:
        """Launch Claude Code with overlay context and auto-detected skills."""
        overlay_root = project_path or _find_project_root()
        lines = [f"You are working on the {overlay_name} TeaTree overlay project.", ""]
        if project_path:
            lines.append(f"Overlay source: {project_path}")
        _launch_claude(task=task, project_root=overlay_root, context_lines=lines)

    # Config and autostart commands
    _register_config_commands(overlay_app, overlay_name, project_path)

    # Register overlay command groups with individual subcommands
    for group_name, (help_text, subcommands) in _DJANGO_GROUPS.items():
        group = typer.Typer(no_args_is_help=True, help=help_text)
        for sub_name, sub_help in subcommands:
            _bridge_subcommand(group, group_name, sub_name, sub_help, project_path)
        overlay_app.add_typer(group, name=group_name)

    # Register overlay-specific tool commands
    _register_overlay_tools(overlay_app, project_path)

    return overlay_app


def _bridge_subcommand(
    group: typer.Typer,
    group_name: str,
    sub_name: str,
    sub_help: str,
    project_path: Path | None,
) -> None:
    """Register a single subcommand that forwards to ``manage.py <group> <sub>``."""

    @group.command(
        name=sub_name,
        context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
        help=sub_help,
    )
    def _run(ctx: typer.Context) -> None:
        _managepy(project_path, group_name, sub_name, *ctx.args)

    _run.__name__ = f"_run_{group_name}_{sub_name.replace('-', '_')}"


def _register_config_commands(
    overlay_app: typer.Typer,
    overlay_name: str,
    project_path: Path | None,
) -> None:
    """Register ``config`` subgroup with autostart and log commands."""
    config_group = typer.Typer(no_args_is_help=True, help="Overlay configuration.")

    @config_group.command(name="enable-autostart")
    def enable_autostart(
        host: str = typer.Option("127.0.0.1", help="Host to bind to"),
        port: int = typer.Option(8000, help="Port to serve on"),
    ) -> None:
        """Install a system daemon to start the dashboard on login."""
        from teetree.autostart import enable  # noqa: PLC0415
        from teetree.config import discover_active_overlay  # noqa: PLC0415

        active = discover_active_overlay()
        settings_module = active.settings_module if active else ""
        msg = enable(
            overlay_name=overlay_name,
            project_path=project_path or Path.cwd(),
            settings_module=settings_module,
            host=host,
            port=port,
        )
        typer.echo(msg)

    @config_group.command(name="disable-autostart")
    def disable_autostart() -> None:
        """Remove the dashboard autostart daemon."""
        from teetree.autostart import disable  # noqa: PLC0415

        msg = disable(overlay_name=overlay_name)
        typer.echo(msg)

    @config_group.command(name="logs")
    def show_logs(
        lines: int = typer.Option(50, "--lines", "-n", help="Number of lines to show"),
        *,
        follow: bool = typer.Option(default=False, help="Follow log output"),
        stderr: bool = typer.Option(default=False, help="Show stderr log instead of stdout"),
    ) -> None:
        """Show dashboard daemon log output."""
        from teetree.autostart import log_paths  # noqa: PLC0415

        paths = log_paths(overlay_name=overlay_name)
        log_file = paths["stderr"] if stderr else paths["stdout"]

        if not log_file.is_file():
            typer.echo(f"No log file found at {log_file}")
            raise typer.Exit(code=1)

        if follow:
            subprocess.run(  # noqa: S603
                ["tail", "-f", "-n", str(lines), str(log_file)],  # noqa: S607
                check=False,
            )
        else:
            subprocess.run(  # noqa: S603
                ["tail", "-n", str(lines), str(log_file)],  # noqa: S607
                check=False,
            )

    overlay_app.add_typer(config_group, name="config")


def _register_overlay_tools(overlay_app: typer.Typer, project_path: Path | None) -> None:
    """Register tool commands from ``hook-config/tool-commands.json`` files."""
    if project_path is None:
        return

    import json as _json  # noqa: PLC0415

    tool_commands: list[dict[str, str]] = []
    for candidate in project_path.rglob("hook-config/tool-commands.json"):
        try:
            data = _json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, list):  # pragma: no branch
                tool_commands.extend(data)
        except Exception:  # noqa: BLE001, S112
            continue

    if not tool_commands:
        return

    tool_group = typer.Typer(no_args_is_help=True, help="Overlay-specific utilities.")
    for tool_spec in tool_commands:
        name = tool_spec.get("name", "")
        help_text = tool_spec.get("help", "")
        mgmt_cmd = tool_spec.get("management_command", "")
        if not name or not mgmt_cmd:
            continue
        _bridge_tool_command(tool_group, name, help_text, mgmt_cmd, project_path)
    overlay_app.add_typer(tool_group, name="tool")


def _bridge_tool_command(
    group: typer.Typer,
    name: str,
    help_text: str,
    management_command: str,
    project_path: Path | None,
) -> None:
    """Register a tool subcommand that forwards to a management command."""

    @group.command(
        name=name,
        context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
        help=help_text,
    )
    def _run(ctx: typer.Context) -> None:
        _managepy(project_path, *management_command.split(), *ctx.args)

    _run.__name__ = f"_run_tool_{name.replace('-', '_')}"


# ── Introspection helpers ─────────────────────────────────────────────


def _print_package_info(dist_name: str, import_name: str, *, label: str = "") -> None:
    label = label or dist_name

    try:
        import importlib  # noqa: PLC0415

        mod = importlib.import_module(import_name)
        source_path = getattr(mod, "__file__", None) or ""
        source_dir = str(Path(source_path).parent) if source_path else "(unknown)"
    except ImportError:
        typer.echo(f"{label + ':':<18}not installed")
        typer.echo()
        return

    editable, url = _editable_info(dist_name)
    mode = "editable" if editable else "installed"
    typer.echo(f"{label + ':':<18}{source_dir}  ({mode})")
    if editable and url:  # pragma: no branch
        typer.echo(f"{'':18}{url}")
    typer.echo()


def _editable_info(dist_name: str) -> tuple[bool, str]:
    """Return (is_editable, source_url) for a distribution."""
    import json  # noqa: PLC0415
    from importlib.metadata import PackageNotFoundError, distribution  # noqa: PLC0415

    try:
        dist = distribution(dist_name)
    except PackageNotFoundError:
        return False, ""

    direct_url = dist.read_text("direct_url.json")
    if not direct_url:
        return False, ""

    try:
        data = json.loads(direct_url)
    except (json.JSONDecodeError, AttributeError):
        return False, ""
    else:
        editable = data.get("dir_info", {}).get("editable", False)
        url = data.get("url", "")
        return editable, url


# ── Entry point ──────────────────────────────────────────────────────


def main() -> None:
    """Entry point for the ``t3`` console script."""
    _register_overlay_commands()
    app(standalone_mode=True)
