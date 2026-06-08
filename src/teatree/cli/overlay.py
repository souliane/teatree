"""Overlay CLI — builds Typer sub-apps that delegate to manage.py commands."""

import json as _json
import logging
import os
import sys
from pathlib import Path

import typer

from teatree.cli.autonomy import register_autonomy_commands
from teatree.cli.django_groups import DJANGO_GROUPS, DjangoGroup
from teatree.cli.speed import register_speed_commands
from teatree.cli.teatree_gate import register_gate_commands
from teatree.utils.django_db import runner_prefix
from teatree.utils.run import run_streamed, spawn
from teatree.utils.singleton import AlreadyRunningError, singleton

logger = logging.getLogger(__name__)

# Re-exported for consumers that import the catalogue from this module
# (the CLI reference generator, tests): the data lives in
# ``teatree.cli.django_groups`` but ``overlay`` stays its public home.
__all__ = ["DJANGO_GROUPS", "OVERLAY_PROXY_COMMANDS", "DjangoGroup", "OverlayAppBuilder", "managepy", "managepy_core"]


def _managepy_cmd(project_path: Path, *args: str) -> list[str]:
    """Build the ``manage.py`` invocation for *project_path* via the shared prefix.

    Routes through :func:`teatree.utils.django_db.runner_prefix` — the single
    site that emits the interpreter prefix — so the overlay ``db_worker`` /
    ``managepy`` paths inherit the pipenv-vs-uv detection instead of an
    unconditional ``uv --directory`` (souliane/teatree#1976, #1973).
    """
    return [*runner_prefix(project_path), *args]


OVERLAY_PROXY_COMMANDS: dict[str, tuple[str, str]] = {}
"""Maps proxy callback ``__name__`` -> ``(django_group, django_sub)``.

Populated in :meth:`OverlayAppBuilder._bridge_subcommand`.  Consumed by the
CLI reference generator to swap the proxy's stub help for the underlying
``TyperCommand``'s real click tree.  The proxy function's ``__name__`` is
reassigned per-leaf (``_run_{group}_{sub}``); object identity is not stable
across Typer's ``get_command`` conversion.
"""


def _base_env() -> dict[str, str]:
    """Build a clean environment dict, stripping DJANGO_SETTINGS_MODULE."""
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    # Preserve the user's original shell CWD so resolve_worktree() can
    # auto-detect the worktree even though manage.py runs from the overlay dir.
    env["T3_ORIG_CWD"] = os.environ.get("PWD", str(Path.cwd()))
    return env


def _run_workers(project_path: Path, overlay_name: str, count: int, interval: float) -> None:
    """Spawn *count* ``db_worker`` subprocesses and block until they exit."""
    manage_py = str(project_path / "manage.py")
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    if overlay_name:
        env["T3_OVERLAY_NAME"] = overlay_name
    processes = [
        spawn(
            [
                *_managepy_cmd(project_path, manage_py, "db_worker"),
                "--interval",
                str(interval),
                "--no-startup-delay",
                "--no-reload",
            ],
            cwd=project_path,
            env=env,
        )
        for _ in range(count)
    ]
    typer.echo(f"Started {count} worker(s). Press Ctrl+C to stop.")
    try:
        for p in processes:
            p.wait()
    except KeyboardInterrupt:
        typer.echo("Shutting down workers...")
        for p in processes:
            p.terminate()
        for p in processes:
            p.wait(timeout=5)


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
        cmd = _managepy_cmd(project_path, "manage.py", *args)
        run_streamed(cmd, cwd=project_path, env=env)
    else:
        env.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
        run_streamed([sys.executable, "-m", "teatree", *args], env=env)


def managepy_core(*args: str, overlay_name: str = "") -> None:
    """Run a teatree-CORE management command via ``python -m teatree``.

    Use this for commands that live in ``teatree.core.management.commands`` —
    ``followup``, ``review_request_check``, ``review_request_post``, etc.
    These exist on teatree core, not on overlay-owned ``manage.py`` projects
    (an overlay clone may run against its own settings module that has no
    such commands). Routing them through :func:`managepy` would crash when
    invoked from such a clone, because :func:`managepy` prefers the overlay's
    ``manage.py`` whenever the resolved project path has one (#1312).

    When *overlay_name* is provided, ``T3_OVERLAY_NAME`` is set in the subprocess
    environment so that ``get_overlay()`` can resolve the correct overlay even
    when multiple overlays are installed.
    """
    env = _base_env()
    if overlay_name:
        env["T3_OVERLAY_NAME"] = overlay_name
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
        overlay_name = self.overlay_name

        @self.overlay_app.callback(invoke_without_command=True)
        def _activate() -> None:
            os.environ["T3_OVERLAY_NAME"] = overlay_name

        self._register_resetdb_command()
        self._register_worker_command()
        self._register_shortcut_commands()
        self._register_config_commands()
        register_gate_commands(self.overlay_app)
        register_speed_commands(self.overlay_app)
        register_autonomy_commands(self.overlay_app)

        for group_name, dj_group in DJANGO_GROUPS.items():
            group = typer.Typer(no_args_is_help=True, help=dj_group.help_text)
            for sub_name, sub_help in dj_group.subcommands:
                self._bridge_subcommand(
                    group,
                    group_name,
                    sub_name,
                    sub_help,
                    core_dispatch=dj_group.dispatches_to_core(sub_name),
                )
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
            from teatree.paths import CANONICAL_DB  # noqa: PLC0415

            if CANONICAL_DB.exists():
                CANONICAL_DB.unlink()
                typer.echo(f"Deleted {CANONICAL_DB}")
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
            """Start background task workers.

            Singleton across the machine: a second invocation refuses to start
            while one is alive, since both would drain the same canonical DB.
            """
            if project_path is None:
                typer.echo("Cannot find overlay project directory.")
                raise typer.Exit(code=1)

            try:
                with singleton("teatree-worker"):
                    _run_workers(project_path, overlay_name, count, interval)
            except AlreadyRunningError as exc:
                typer.echo(f"WARN  {exc}. Stop it before starting another.")
                raise typer.Exit(code=1) from None

    def _register_shortcut_commands(self) -> None:
        """Register overlay-scoped workflow shortcuts."""
        project_path = self.project_path
        overlay_name = self.overlay_name
        overlay_app = self.overlay_app

        @overlay_app.command(name="full-status")
        def full_status() -> None:
            """Show ticket, worktree, and session state summary."""
            # ``followup`` is a teatree-CORE management command — dispatch via
            # ``python -m teatree`` so an overlay clone with its own
            # ``manage.py`` (different settings module) does not crash with
            # ``Unknown command: 'followup'`` (#1318).
            managepy_core("followup", "refresh", overlay_name=overlay_name)

        @overlay_app.command(name="ship")
        def ship(
            ticket_id: str = typer.Argument(help="Ticket ID"),
            title: str = typer.Option("", help="PR title"),
        ) -> None:
            """Code to PR — create pull request for the ticket."""
            args = ["pr", "create", ticket_id]
            if title:
                args.extend(["--title", title])
            managepy(project_path, *args, overlay_name=overlay_name)

        @overlay_app.command(name="daily")
        def daily() -> None:
            """Daily followup — sync MRs, check gates, remind reviewers."""
            # Same as ``full-status``: ``followup`` is core-only (#1318).
            managepy_core("followup", "sync", overlay_name=overlay_name)

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
            from teatree.skill_support.loading import SkillLoadingPolicy  # noqa: PLC0415

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
        *,
        core_dispatch: bool = False,
    ) -> None:
        """Register a single subcommand that forwards to ``manage.py <group> <sub>``.

        Uses ``invoke_without_command=True`` and disables the default typer help
        so that ``--help`` is forwarded to the Django management command, which
        shows the real options (``--path``, ``--variant``, etc.).

        When ``core_dispatch`` is ``True`` the command is dispatched via
        :func:`managepy_core` (teatree-native ``python -m teatree``) instead
        of :func:`managepy` — required for groups whose commands live in
        teatree core only and would crash if routed through an overlay's
        own ``manage.py`` (#1312, #1318).
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
            if core_dispatch:
                managepy_core(group_name, sub_name, *ctx.args, overlay_name=overlay_name)
            else:
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
