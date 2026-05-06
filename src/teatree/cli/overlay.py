"""Overlay CLI — builds Typer sub-apps that delegate to manage.py commands."""

import json as _json
import logging
import os
import shutil
import sys
from pathlib import Path

import typer

from teatree.utils.run import run_streamed, spawn

logger = logging.getLogger(__name__)

OVERLAY_PROXY_COMMANDS: dict[str, tuple[str, str]] = {}
"""Maps proxy callback ``__name__`` -> ``(django_group, django_sub)``.

Populated in :meth:`OverlayAppBuilder._bridge_subcommand`.  Consumed by the
CLI reference generator to swap the proxy's stub help for the underlying
``TyperCommand``'s real click tree.  The proxy function's ``__name__`` is
reassigned per-leaf (``_run_{group}_{sub}``); object identity is not stable
across Typer's ``get_command`` conversion.
"""


DJANGO_GROUPS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "worktree": (
        "Per-worktree FSM operations.",
        [
            ("provision", "Run DB import + env cache + direnv + prek + overlay setup steps for one worktree."),
            ("start", "Boot ``docker compose up`` for one worktree."),
            ("verify", "Run overlay health checks for one worktree."),
            ("ready", "Run runtime readiness probes for one worktree."),
            ("teardown", "Stop docker, drop DB, remove git worktree, delete row."),
            ("status", "Report FSM state, branch, and allocated host ports for one worktree."),
            ("diagnose", "Print a structured health checklist for one worktree."),
            ("smoke-test", "Quick health check: overlay loads, CLI responds, imports OK."),
            ("diagram", "Print a state diagram as Mermaid. Models: worktree, ticket, task."),
        ],
    ),
    "workspace": (
        "Ticket-level workspace operations (every worktree in the ticket).",
        [
            ("ticket", "Create or update a ticket and trigger worktree provisioning."),
            ("provision", "Provision every worktree in the current ticket workspace."),
            ("start", "Start docker for every worktree in the current ticket workspace."),
            ("ready", "Run readiness probes for every worktree in the ticket workspace."),
            ("teardown", "Tear down every worktree in the current ticket workspace."),
            ("finalize", "Squash worktree commits and rebase on the default branch."),
            ("doctor", "Detect state drift across every store; optionally fix it."),
            ("clean-merged", "Tear down every worktree whose ticket is already MERGED."),
            ("clean-all", "Prune merged worktrees, stale branches, orphaned stashes, orphan DBs, old DSLR snapshots."),
            ("list-orphans", "List orphan branches (commits not on main, no open PR)."),
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
        ],
    ),
    "e2e": (
        "E2E test commands.",
        [
            ("run", "Run E2E tests — dispatches to project or external runner based on overlay config."),
            ("trigger-ci", "Trigger E2E tests on a remote CI pipeline."),
            ("external", "Run Playwright tests from the external test repo (T3_PRIVATE_TESTS)."),
            ("project", "Run E2E tests from the project's own test directory."),
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
            ("ensure-pr", "Create a PR for an orphan branch (idempotent)."),
            ("check-gates", "Check whether session gates allow a phase transition."),
            ("fetch-issue", "Fetch issue details from the configured tracker."),
            ("detect-tenant", "Detect the current tenant variant from the overlay."),
            ("post-evidence", "Post test evidence as an MR comment."),
            ("sweep", "List your open PRs across the forge for the /t3:sweeping-prs skill."),
        ],
    ),
    "tasks": (
        "Async task queue.",
        [
            ("cancel", "Cancel a task by ID."),
            ("claim", "Claim the next available task."),
            ("create", "Enqueue the next-phase task for a ticket."),
            ("list", "List tasks with optional filters."),
            ("start", "Claim and run the next interactive task in the current terminal."),
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
    "lifecycle": (
        "Session lifecycle and phase tracking.",
        [
            ("visit-phase", "Mark a phase as visited on the ticket's latest session."),
        ],
    ),
}


def uv_cmd(project_path: Path, *args: str) -> list[str]:
    """Build a ``uv --directory <path> run ...`` command list."""
    uv = shutil.which("uv") or "uv"
    return [uv, "--directory", str(project_path), "run", *args]


def _base_env() -> dict[str, str]:
    """Build a clean environment dict, stripping DJANGO_SETTINGS_MODULE."""
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    # Preserve the user's original shell CWD so resolve_worktree() can
    # auto-detect the worktree even though manage.py runs from the overlay dir.
    env["T3_ORIG_CWD"] = os.environ.get("PWD", str(Path.cwd()))
    return env


def managepy(project_path: Path | None, *args: str, overlay_name: str = "") -> None:
    """Run a Django management command for an overlay.

    For overlays with their own project directory (TOML-configured), delegates
    to ``uv --directory <path> run python manage.py``.  For entry-point overlays
    (pip-installed, no project directory), uses ``python -m teatree``.

    When *overlay_name* is provided, ``T3_OVERLAY_NAME`` is set in the subprocess
    environment so that ``get_overlay()`` can resolve the correct overlay even when
    multiple overlays are installed.
    """
    env = _base_env()
    if overlay_name:
        env["T3_OVERLAY_NAME"] = overlay_name

    if project_path and (project_path / "manage.py").is_file():
        cmd = uv_cmd(project_path, "python", "manage.py", *args)
        run_streamed(cmd, cwd=project_path, env=env)
    else:
        env.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
        run_streamed([sys.executable, "-m", "teatree", *args], env=env)


class OverlayAppBuilder:
    """Build a Typer sub-app for a single installed overlay."""

    def __init__(self, overlay_name: str, project_path: Path | None, settings_module: str = "") -> None:
        self.overlay_name = overlay_name
        self.project_path = project_path
        self.settings_module = settings_module
        self.overlay_app = typer.Typer(no_args_is_help=True, help=f"Commands for the {overlay_name} overlay.")

    def build(self) -> typer.Typer:
        """Build and return the fully-configured overlay Typer app."""
        self._register_resetdb_command()
        self._register_worker_command()
        self._register_shortcut_commands()
        self._register_config_commands()

        for group_name, (help_text, subcommands) in DJANGO_GROUPS.items():
            group = typer.Typer(no_args_is_help=True, help=help_text)
            for sub_name, sub_help in subcommands:
                self._bridge_subcommand(group, group_name, sub_name, sub_help)
            self.overlay_app.add_typer(group, name=group_name)

        self._register_overlay_tools()
        return self.overlay_app

    def _register_resetdb_command(self) -> None:
        """Register the resetdb command on the overlay sub-app."""
        project_path = self.project_path
        overlay_name = self.overlay_name
        overlay_app = self.overlay_app

        @overlay_app.command()
        def resetdb() -> None:
            """Drop the SQLite database and re-run all migrations."""
            from teatree.config import get_data_dir  # noqa: PLC0415

            db_path = get_data_dir(overlay_name) / "db.sqlite3"
            if db_path.exists():
                db_path.unlink()
                typer.echo(f"Deleted {db_path}")
            managepy(project_path, "migrate", "--no-input", overlay_name=overlay_name)
            typer.echo("Database recreated.")

    def _register_worker_command(self) -> None:
        """Register the background worker command."""
        project_path = self.project_path
        overlay_name = self.overlay_name
        overlay_app = self.overlay_app

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
            if overlay_name:
                env["T3_OVERLAY_NAME"] = overlay_name
            processes = []
            for _ in range(count):
                p = spawn(
                    [
                        *uv_cmd(project_path, "python", manage_py, "db_worker"),
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

    def _register_shortcut_commands(self) -> None:
        """Register overlay-scoped workflow shortcuts."""
        project_path = self.project_path
        overlay_name = self.overlay_name
        overlay_app = self.overlay_app

        @overlay_app.command(name="full-status")
        def full_status() -> None:
            """Show ticket, worktree, and session state summary."""
            managepy(project_path, "followup", "refresh", overlay_name=overlay_name)

        @overlay_app.command(name="ship")
        def ship(
            ticket_id: str = typer.Argument(help="Ticket ID"),
            title: str = typer.Option("", help="MR title"),
        ) -> None:
            """Code to MR — create merge request for the ticket."""
            args = ["pr", "create", ticket_id]
            if title:
                args.extend(["--title", title])
            managepy(project_path, *args, overlay_name=overlay_name)

        @overlay_app.command(name="daily")
        def daily() -> None:
            """Daily followup — sync MRs, check gates, remind reviewers."""
            managepy(project_path, "followup", "sync", overlay_name=overlay_name)

        self._register_agent_command()

    def _register_agent_command(self) -> None:
        """Register the ``agent`` overlay command."""
        project_path = self.project_path
        overlay_name = self.overlay_name

        @self.overlay_app.command(name="agent")
        def overlay_agent(
            task: str = typer.Argument("", help="What to work on"),
            phase: str = typer.Option("", "--phase", help="Explicit TeaTree phase override."),
            skill: list[str] = typer.Option(
                None,
                "--skill",
                help="Explicit skill override. Repeat to load multiple skills.",
            ),
        ) -> None:
            """Launch Claude Code with overlay context and auto-detected skills."""
            from teatree.cli import _find_project_root  # noqa: PLC0415
            from teatree.cli.agent import _detect_agent_ticket_status, _launch_claude  # noqa: PLC0415
            from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415
            from teatree.skill_loading import SkillLoadingPolicy  # noqa: PLC0415

            overlay_root = project_path or _find_project_root()
            if phase and skill:
                typer.echo("--phase and --skill cannot be used together.")
                raise typer.Exit(code=1)
            lines = [f"You are working on the {overlay_name} TeaTree overlay project.", ""]
            if project_path:
                lines.append(f"Overlay source: {project_path}")
            selection = SkillLoadingPolicy().select_for_agent_launch(
                cwd=Path.cwd(),
                overlay_skill_metadata=get_overlay().metadata.get_skill_metadata(),
                task=task,
                ticket_status=_detect_agent_ticket_status(overlay_root),
                explicit_phase=phase,
                explicit_skills=skill or [],
                overlay_active=True,
            )
            _launch_claude(
                task=task,
                project_root=overlay_root,
                context_lines=lines,
                skills=selection.skills,
                ask_user_which_skill=selection.ask_user,
            )

    def _register_config_commands(self) -> None:
        """Register the empty ``config`` subgroup so overlay commands hang off it."""
        config_group = typer.Typer(no_args_is_help=True, help="Overlay configuration.")
        self.overlay_app.add_typer(config_group, name="config")

    def _bridge_subcommand(
        self,
        group: typer.Typer,
        group_name: str,
        sub_name: str,
        sub_help: str,
    ) -> None:
        """Register a single subcommand that forwards to ``manage.py <group> <sub>``.

        Uses ``invoke_without_command=True`` and disables the default typer help
        so that ``--help`` is forwarded to the Django management command, which
        shows the real options (``--path``, ``--variant``, etc.).
        """
        project_path = self.project_path
        overlay_name = self.overlay_name

        @group.command(
            name=sub_name,
            context_settings={
                "allow_extra_args": True,
                "allow_interspersed_args": False,
                "ignore_unknown_options": True,
            },
            help=sub_help,
            add_help_option=False,
        )
        def _run(ctx: typer.Context) -> None:
            managepy(project_path, group_name, sub_name, *ctx.args, overlay_name=overlay_name)

        _run.__name__ = f"_run_{group_name}_{sub_name.replace('-', '_')}"
        OVERLAY_PROXY_COMMANDS[_run.__name__] = (group_name, sub_name)

    def _register_overlay_tools(self) -> None:
        """Register tool commands from ``skills/*/hook-config/tool-commands.json`` files.

        Bounded to the documented per-overlay layout (one skill dir per package),
        which is fast and side-steps an unbounded ``rglob`` over the entire
        project tree (``.venv/``, ``__pycache__/``, ``node_modules/``, etc.).
        """
        project_path = self.project_path
        if project_path is None:
            return

        tool_commands: list[dict[str, str]] = []
        for candidate in project_path.glob("skills/*/hook-config/tool-commands.json"):
            try:
                data = _json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, list):  # pragma: no branch
                    tool_commands.extend(data)
            except _json.JSONDecodeError:
                logger.warning("Invalid JSON in %s", candidate)
                continue
            except OSError as exc:
                logger.warning("Cannot read %s: %s", candidate, exc)
                continue

        if not tool_commands:
            return

        tool_group = typer.Typer(no_args_is_help=True, help="Overlay-specific utilities.")
        for tool_spec in tool_commands:
            name = tool_spec.get("name", "")
            help_text = tool_spec.get("help", "")
            mgmt_cmd = tool_spec.get("command", "")
            if not name or not mgmt_cmd:
                continue
            self._bridge_tool_command(tool_group, name, help_text, mgmt_cmd)
        self.overlay_app.add_typer(tool_group, name="tool")

    def _bridge_tool_command(
        self,
        group: typer.Typer,
        name: str,
        help_text: str,
        command: str,
    ) -> None:
        """Register a tool subcommand that forwards to a shell command."""
        project_path = self.project_path
        overlay_name = self.overlay_name

        @group.command(
            name=name,
            context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
            help=help_text,
        )
        def _run(ctx: typer.Context) -> None:
            managepy(project_path, *command.split(), *ctx.args, overlay_name=overlay_name)

        _run.__name__ = f"_run_tool_{name.replace('-', '_')}"
