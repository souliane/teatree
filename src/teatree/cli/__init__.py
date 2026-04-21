"""TeaTree CLI — single ``t3`` entry point for all commands.

DB-touching commands are django-typer management commands, exposed here after
``django.setup()``.  Django-free commands live as plain Typer groups.
"""

import contextlib
import logging
import os
import signal
import sys
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.utils.run import Popen

from teatree.cli.assess import assess_app
from teatree.cli.ci import ci_app
from teatree.cli.doctor import DoctorService, IntrospectionHelpers, doctor_app
from teatree.cli.infra import infra_app
from teatree.cli.overlay import OverlayAppBuilder, _uvicorn, managepy
from teatree.cli.overlay_dev import overlay_dev_app
from teatree.cli.review import review_app
from teatree.cli.setup import setup_app
from teatree.cli.tools import tool_app
from teatree.config import discover_active_overlay
from teatree.utils.run import run_streamed, spawn

logger = logging.getLogger(__name__)

__all__ = ["app", "main"]

app = typer.Typer(name="t3", no_args_is_help=True, add_completion=False)

AGENT_PHASE_OPTION = typer.Option("", "--phase", help="Explicit TeaTree phase override.")
AGENT_SKILL_OPTION = typer.Option(
    None,
    "--skill",
    help="Explicit skill override. Repeat to load multiple skills.",
)

# ── Always-available commands (no Django) ──────────────────────────────


@app.callback()
def _root_callback(ctx: typer.Context) -> None:
    ctx.ensure_object(dict)
    _maybe_show_update_notice()


def _maybe_show_update_notice() -> None:
    """Show update notice at most once per day, if enabled in user settings."""
    try:
        from teatree.config import check_for_updates  # noqa: PLC0415

        message = check_for_updates()
        if message:
            typer.echo(f"[update] {message}", err=True)
    except Exception:  # noqa: BLE001, S110
        pass


@app.command()
def startoverlay(
    project_name: str,
    destination: Path,
    *,
    overlay_app: str = typer.Option("t3_overlay", "--overlay-app", help="Name of the overlay Django app"),
    project_package: str | None = typer.Option(
        None,
        "--project-package",
        help="Project package name (default: derived from project name)",
    ),
) -> None:
    """Create a new TeaTree overlay package."""
    from teatree.overlay_init.generator import OverlayScaffolder  # noqa: PLC0415

    project_root = destination / project_name
    if project_root.exists():
        typer.echo(f"Destination already exists: {project_root}")
        raise typer.Exit(code=1)

    package_name = project_package or project_name.replace("-", "_").replace("t3_", "")
    scaffolder = OverlayScaffolder(project_root, overlay_app, package_name)
    scaffolder.scaffold(project_name)
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
    run_streamed(
        [sys.executable, "-m", "mkdocs", "serve", "-a", f"{host}:{port}"],
        cwd=project_root,
    )


def _detect_agent_ticket_status(project_root: Path) -> str:
    if not (project_root / "manage.py").is_file():
        return ""
    try:
        import django  # noqa: PLC0415

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
        django.setup()
        from teatree.core.resolve import resolve_worktree  # noqa: PLC0415

        return str(resolve_worktree().ticket.state)
    except Exception:
        logger.debug("Failed to detect agent ticket status", exc_info=True)
        return "(error)"


def _launch_claude(
    *,
    task: str,
    project_root: Path,
    context_lines: list[str],
    skills: list[str],
    ask_user_which_skill: bool,
) -> None:
    """Shared logic: resolve skills, build prompt, exec into claude."""
    import shutil  # noqa: PLC0415

    claude_bin = shutil.which("claude")
    if not claude_bin:
        typer.echo("claude CLI not found on PATH. Install Claude Code first.")
        raise typer.Exit(code=1)

    teatree_editable, teatree_url = IntrospectionHelpers.editable_info("teatree")
    if teatree_editable and teatree_url:
        context_lines.append(f"TeaTree source (editable): {teatree_url.removeprefix('file://')}")
    context_lines.append("")
    if skills:
        context_lines.extend(
            (
                "Load only these skills before starting work:",
                *(f"  - /{skill}" for skill in skills),
            ),
        )
    if ask_user_which_skill:
        context_lines.extend(
            (
                "TeaTree could not infer the lifecycle skill for this session.",
                "Before doing any work, ask the user which lifecycle skill to load.",
            ),
        )
    context_lines.extend(("", "Run `t3 --help` to see available commands.", "Run `uv run pytest` to run tests."))
    if task:
        context_lines.extend(("", f"Task: {task}"))

    context = "\n".join(context_lines)
    cmd = [claude_bin, "--append-system-prompt", context]

    if os.environ.get("T3_CONTRIBUTE", "").lower() == "true":
        from teatree import find_project_root  # noqa: PLC0415

        teatree_root = find_project_root()
        if teatree_root:
            cmd.extend(["--plugin-dir", str(teatree_root)])

    if task:
        cmd.extend(["-p", task])

    typer.echo(f"Launching Claude Code in {project_root}...")
    os.execvp(claude_bin, cmd)  # noqa: S606


@app.command()
def agent(
    task: str = typer.Argument("", help="What to work on (e.g. 'fix the sync bug', 'add a new command')"),
    phase: str = AGENT_PHASE_OPTION,
    skill: list[str] = AGENT_SKILL_OPTION,
) -> None:
    """Launch Claude Code with auto-detected project context."""
    from teatree.config import discover_active_overlay  # noqa: PLC0415
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415
    from teatree.skill_loading import SkillLoadingPolicy  # noqa: PLC0415

    project_root = _find_project_root()
    active = discover_active_overlay()
    if phase and skill:
        typer.echo("--phase and --skill cannot be used together.")
        raise typer.Exit(code=1)

    lines = ["You are working on a TeaTree project.", ""]
    if active:
        lines.extend(
            (
                f"Active overlay: {active.name} ({active.overlay_class or '(cwd)'})",
                f"Overlay source: {project_root}",
            ),
        )
    else:
        lines.append("No overlay active — working on teatree itself.")

    overlay_skill_metadata = get_overlay().metadata.get_skill_metadata() if active else {}
    policy = SkillLoadingPolicy()
    try:
        selection = policy.select_for_agent_launch(
            cwd=Path.cwd(),
            overlay_skill_metadata=overlay_skill_metadata,
            task=task,
            ticket_status=_detect_agent_ticket_status(project_root) if active else "",
            explicit_phase=phase,
            explicit_skills=skill or [],
            overlay_active=bool(active),
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    _launch_claude(
        task=task,
        project_root=project_root,
        context_lines=lines,
        skills=selection.skills,
        ask_user_which_skill=selection.ask_user,
    )


_MILLISECOND_TIMESTAMP_THRESHOLD = 1e12
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400
_PROMPT_DISPLAY_MAX = 80
_PROMPT_DISPLAY_TRUNCATE = 77


def _format_session_age(raw_ts: float | str, now: float) -> str:
    if isinstance(raw_ts, str):
        try:
            raw_ts = float(raw_ts)
        except (ValueError, TypeError):
            raw_ts = 0
    ts = raw_ts / 1000 if raw_ts > _MILLISECOND_TIMESTAMP_THRESHOLD else raw_ts
    if not ts:
        return "?"
    age_s = now - ts
    if age_s < _SECONDS_PER_HOUR:
        return f"{int(age_s / 60)}m ago"
    if age_s < _SECONDS_PER_DAY:
        return f"{int(age_s / _SECONDS_PER_HOUR)}h ago"
    return f"{int(age_s / _SECONDS_PER_DAY)}d ago"


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

    from teatree.claude_sessions import SessionQuery, list_sessions  # noqa: PLC0415

    results = list_sessions(
        SessionQuery(
            project_filter=project,
            all_projects=all_projects,
            limit=limit,
        ),
    )

    if not results:
        typer.echo("No sessions found.")
        raise typer.Exit

    now = datetime.now(tz=UTC).timestamp()
    for r in results:
        age = _format_session_age(r.timestamp, now)
        prompt = r.first_prompt.replace("\n", " ").strip()
        if len(prompt) > _PROMPT_DISPLAY_MAX:
            prompt = prompt[:_PROMPT_DISPLAY_TRUNCATE] + "..."

        status_label = "done" if r.status == "finished" else r.status

        typer.echo(f"\n  {age:<8} [{status_label}] {r.project}")
        if prompt:
            typer.echo(f"           {prompt}")
        if r.status != "finished":
            resume = f"claude --resume {r.session_id}"
            typer.echo(f"           {f'cd {r.cwd} && {resume}' if r.cwd else resume}")

    typer.echo("")


# ── Top-level info ─────────────────────────────────────────────────────


@app.command()
def info() -> None:
    """Show t3 installation, teatree/overlay sources, and editable status."""
    DoctorService.show_info()


config_app = typer.Typer(no_args_is_help=True, help="Configuration and autoloading.")
app.add_typer(config_app, name="config")


@config_app.command(name="check-update")
def check_update() -> None:
    """Check if a newer version of teatree is available."""
    from teatree.config import check_for_updates  # noqa: PLC0415

    message = check_for_updates(force=True)
    typer.echo(message or "You are up to date.")


@config_app.command(name="write-skill-cache")
def write_skill_cache() -> None:
    """Write overlay skill metadata to XDG cache for hook consumption."""
    import json as _json  # noqa: PLC0415

    import django  # noqa: PLC0415

    from teatree.config import DATA_DIR, discover_active_overlay  # noqa: PLC0415

    active = discover_active_overlay()
    if active and "DJANGO_SETTINGS_MODULE" not in os.environ:
        os.environ["DJANGO_SETTINGS_MODULE"] = "teatree.settings"
    django.setup()

    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    overlay = get_overlay()
    metadata = overlay.metadata.get_skill_metadata()
    cache_path = DATA_DIR / "skill-metadata.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(_json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"Wrote skill metadata to {cache_path}")


@config_app.command()
def autoload() -> None:
    """List skill auto-loading rules from context-match.yml files."""
    from teatree.agents.skill_bundle import DEFAULT_SKILLS_DIR  # noqa: PLC0415

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

    from teatree.config import DATA_DIR  # noqa: PLC0415

    cache_path = DATA_DIR / "skill-metadata.json"
    if not cache_path.is_file():
        typer.echo(f"No cache found at {cache_path}")
        typer.echo("Run: t3 config write-skill-cache")
        raise typer.Exit(code=1)

    data = _json.loads(cache_path.read_text(encoding="utf-8"))
    typer.echo(f"Cache: {cache_path}")
    typer.echo(_json.dumps(data, indent=2))


@config_app.command()
def deps(skill: str) -> None:
    """Show resolved dependency chain for a skill."""
    import json as _json  # noqa: PLC0415

    from teatree.config import DATA_DIR  # noqa: PLC0415
    from teatree.skill_deps import resolve_all  # noqa: PLC0415

    cache_path = DATA_DIR / "skill-metadata.json"
    if not cache_path.is_file():
        typer.echo(f"No cache found at {cache_path}")
        typer.echo("Run: t3 config write-skill-cache")
        raise typer.Exit(code=1)

    data = _json.loads(cache_path.read_text(encoding="utf-8"))
    trigger_index = data.get("trigger_index", [])

    # Use pre-computed resolved_requires if available, otherwise compute.
    precomputed = data.get("resolved_requires", {})
    if precomputed and skill in precomputed:
        chain = precomputed[skill]
    else:
        resolved = resolve_all(trigger_index)
        chain = resolved.get(skill, [skill])

    typer.echo(" → ".join(chain))


@config_app.command(name="test-trigger")
def test_trigger(prompt: str) -> None:
    """Test which skill would be triggered for a given prompt."""
    import json as _json  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415

    # Import from scripts/lib (same pattern as _startup.py).
    from teatree import find_project_root as _find_root  # noqa: PLC0415
    from teatree.config import DATA_DIR  # noqa: PLC0415

    root = _find_root()
    scripts_lib = root / "scripts" / "lib" if root else Path(__file__).resolve().parent
    if str(scripts_lib) not in _sys.path:
        _sys.path.insert(0, str(scripts_lib))

    from skill_loader import detect_intent_detailed  # noqa: PLC0415  # ty: ignore[unresolved-import]

    cache_path = DATA_DIR / "skill-metadata.json"
    trigger_index: list[dict] | None = None
    if cache_path.is_file():
        data = _json.loads(cache_path.read_text(encoding="utf-8"))
        trigger_index = data.get("trigger_index", [])

    match = detect_intent_detailed(prompt, trigger_index=trigger_index)
    typer.echo(str(match))


def _find_overlay_project() -> Path:
    """Find the active overlay project root."""
    from teatree.config import discover_active_overlay  # noqa: PLC0415

    active = discover_active_overlay()
    if active and active.project_path:
        return active.project_path
    return _find_project_root()


class DashboardGuard:
    """Singleton guard for the dashboard server using a PID file."""

    def __init__(self, pid_file: Path) -> None:
        self._pid_file = pid_file

    def _read_pid(self) -> int | None:
        try:
            pid = int(self._pid_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            self._pid_file.unlink(missing_ok=True)
            return None
        return pid

    def stop_existing(self) -> bool:
        """Kill an existing dashboard process. Returns True if one was stopped."""
        pid = self._read_pid()
        if pid is None:
            return False
        typer.echo(f"Stopping existing dashboard (PID {pid})...")
        os.kill(pid, signal.SIGTERM)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
        self._pid_file.unlink(missing_ok=True)
        return True

    def write_pid(self) -> None:
        self._pid_file.parent.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(str(os.getpid()))

    def cleanup(self) -> None:
        self._pid_file.unlink(missing_ok=True)


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", help="Host to bind to"),
    port: int = typer.Option(8000, help="Port to serve on"),
    *,
    project: Path | None = typer.Option(None, help="Project root to serve from (worktree path)."),
    workers: int = typer.Option(1, help="Number of background task workers to start (0 to disable)"),
    stop: bool = typer.Option(False, "--stop", help="Stop the running dashboard and exit."),
) -> None:
    """Migrate the database and start the dashboard dev server."""
    import socket  # noqa: PLC0415

    from teatree.cli.overlay import uv_cmd  # noqa: PLC0415
    from teatree.config import DATA_DIR  # noqa: PLC0415

    guard = DashboardGuard(DATA_DIR / "dashboard.pid")

    if stop:
        if guard.stop_existing():
            typer.echo("Dashboard stopped.")
        else:
            typer.echo("No running dashboard found.")
        return

    guard.stop_existing()

    project_path, overlay_name, settings_module = _resolve_overlay_for_server(project=project)
    managepy(project_path, "migrate", "--no-input", overlay_name=overlay_name)
    actual_port = port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex((host, port)) == 0:
            s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s2.bind((host, 0))
            actual_port = s2.getsockname()[1]
            s2.close()
            typer.echo(f"Port {port} in use, using {actual_port}")

    guard.write_pid()

    worker_procs: list[Popen[str]] = []
    if workers > 0 and project_path:
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        if overlay_name:
            env["T3_OVERLAY_NAME"] = overlay_name
        manage_py = str(project_path / "manage.py")
        worker_cmd = [
            *uv_cmd(project_path, "python", manage_py, "db_worker"),
            "--interval",
            "1",
            "--no-startup-delay",
            "--no-reload",
        ]
        worker_procs.extend(spawn(worker_cmd, cwd=project_path, env=env) for _ in range(workers))
        typer.echo(f"Started {workers} background worker(s).")

    try:
        _uvicorn(project_path, host, actual_port, settings_module, overlay_name=overlay_name)
    finally:
        for p in worker_procs:
            p.terminate()
        for p in worker_procs:
            p.wait(timeout=5)
        guard.cleanup()


def _resolve_overlay_for_server(*, project: Path | None = None) -> tuple[Path, str, str]:
    """Resolve the project path, overlay name, and settings module.

    When *project* is provided it is used as-is — no auto-detection.  Without
    it, auto-detection succeeds only when the result is unambiguous (exactly
    one entry-point overlay, or CWD inside a single project root).  Ambiguous
    situations abort with a clear error asking for ``--project``.
    """
    from teatree.config import discover_overlays  # noqa: PLC0415

    installed = discover_overlays()
    ep_overlays = [e for e in installed if ":" in (e.overlay_class or "")]

    # --- explicit --project: skip auto-detection entirely ---
    if project is not None:
        project_path = project.resolve()
        if not (project_path / "pyproject.toml").is_file():
            typer.echo(f"--project {project_path} does not contain pyproject.toml")
            raise typer.Exit(code=1)
        active = ep_overlays[0] if len(ep_overlays) == 1 else discover_active_overlay()
        overlay_name = active.name if active else ""
        settings_module = "teatree.settings"
        if active and ":" not in (active.overlay_class or "") and active.overlay_class:
            settings_module = active.overlay_class
        return project_path, overlay_name, settings_module

    # --- auto-detection: only when unambiguous ---
    if len(ep_overlays) > 1:
        names = ", ".join(e.name for e in ep_overlays)
        typer.echo(
            f"Multiple overlays installed ({names}). Pass --project <path> to specify which source tree to serve."
        )
        raise typer.Exit(code=1)

    active = ep_overlays[0] if ep_overlays else discover_active_overlay()
    if not active:
        typer.echo("No overlay found. Add one to ~/.teatree.toml or pass --project <path>.")
        raise typer.Exit(code=1)

    project_path = active.project_path
    if not project_path:
        typer.echo(
            f"Overlay '{active.name}' has no project_path configured. Pass --project <path> to specify the source tree."
        )
        raise typer.Exit(code=1)

    overlay_name = active.name
    settings_module = "teatree.settings"
    if ":" not in (active.overlay_class or "") and active.overlay_class:
        settings_module = active.overlay_class
    return project_path, overlay_name, settings_module


def _find_project_root() -> Path:
    """Walk up from cwd to find the project root (contains pyproject.toml)."""
    for directory in [Path.cwd(), *Path.cwd().parents]:
        if (directory / "pyproject.toml").is_file():
            return directory
    return Path.cwd()


# ── Non-Django command groups ──────────────────────────────────────────

app.add_typer(ci_app, name="ci")

app.add_typer(review_app, name="review")

review_request_app = typer.Typer(no_args_is_help=True, help="Batch review requests.")
app.add_typer(review_request_app, name="review-request")

app.add_typer(doctor_app, name="doctor")

app.add_typer(tool_app, name="tool")

app.add_typer(setup_app, name="setup")

app.add_typer(assess_app, name="assess")

app.add_typer(overlay_dev_app, name="overlay")

app.add_typer(infra_app, name="infra")


# ── Review-request commands ──────────────────────────────────────────


@review_request_app.command()
def discover() -> None:
    """Discover open merge requests awaiting review."""
    from teatree.config import discover_active_overlay  # noqa: PLC0415

    active = discover_active_overlay()
    project = active.project_path if active and active.project_path else _find_project_root()
    overlay_name = active.name if active else ""
    managepy(project, "followup", "discover-mrs", overlay_name=overlay_name)


# ── Django-dependent command groups ───────────────────────────────────


def register_overlay_commands(allowlist: set[str] | None = None) -> None:
    """Register all installed overlays as subcommand groups.

    No Django bootstrap needed — commands delegate to manage.py via subprocess.
    Pass *allowlist* of entry names (e.g. ``{"t3-teatree"}``) to register a subset —
    used by the CLI reference generator to keep generated docs deterministic.
    """
    from teatree.config import discover_active_overlay, discover_overlays  # noqa: PLC0415

    active = discover_active_overlay()
    installed = discover_overlays()

    for entry in installed:
        if allowlist is not None and entry.name not in allowlist:
            continue
        short_name = entry.name.removeprefix("t3-")
        project_path = entry.project_path or (active.project_path if active and active.name == entry.name else None)
        # Entry-point overlays use teatree base settings; TOML overlays with their own
        # project dir may have a settings module stored in overlay_class as fallback.
        if project_path and ":" not in entry.overlay_class and entry.overlay_class:
            # Backward compat: TOML overlay with settings module (no ":" means not a class path)
            settings_module = entry.overlay_class
        else:
            settings_module = "teatree.settings"
        overlay_app = OverlayAppBuilder(entry.name, project_path, settings_module).build()
        app.add_typer(overlay_app, name=short_name)


# ── Entry point ──────────────────────────────────────────────────────


def _ensure_editable_if_contributing() -> None:
    """Auto-fix teatree and overlay to editable when contribute=true.

    When the user has ``contribute = true`` in ``~/.teatree.toml``, both
    teatree and the active overlay should be editable so local changes take
    effect immediately.  ``uv sync`` reinstalls from git, undoing this.
    This check runs on every CLI invocation and re-installs if needed.
    """
    try:
        from teatree.config import load_config  # noqa: PLC0415

        if not load_config().user.contribute:
            return

        # Teatree itself
        if not IntrospectionHelpers.editable_info("teatree")[0]:
            repo = DoctorService.find_teatree_repo()
            if repo:
                DoctorService.make_editable("teatree", repo)

        # Active overlay
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

        for overlay_inst in get_all_overlays().values():
            overlay_module = type(overlay_inst).__module__
            top_package = overlay_module.split(".", maxsplit=1)[0]
            from importlib.metadata import packages_distributions  # noqa: PLC0415

            dist_map = packages_distributions()
            dist_names = dist_map.get(top_package, [top_package])
            overlay_dist = dist_names[0] if dist_names else top_package

            is_editable, _ = IntrospectionHelpers.editable_info(overlay_dist)
            if is_editable:
                continue
            # Try to find the overlay repo in the workspace
            overlay_repo = DoctorService.find_overlay_repo(overlay_dist)
            if overlay_repo:
                DoctorService.make_editable(overlay_dist, overlay_repo)
    except Exception:
        logger.debug("editable check skipped", exc_info=True)


def main() -> None:
    """Entry point for the ``t3`` console script."""
    _ensure_editable_if_contributing()
    register_overlay_commands()
    app(standalone_mode=True)
