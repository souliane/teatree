"""Overlay CLI — builds Typer sub-apps that delegate to manage.py commands."""

import json as _json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from teatree.utils.run import run_streamed, spawn
from teatree.utils.singleton import AlreadyRunningError, singleton

logger = logging.getLogger(__name__)

OVERLAY_PROXY_COMMANDS: dict[str, tuple[str, str]] = {}
"""Maps proxy callback ``__name__`` -> ``(django_group, django_sub)``.

Populated in :meth:`OverlayAppBuilder._bridge_subcommand`.  Consumed by the
CLI reference generator to swap the proxy's stub help for the underlying
``TyperCommand``'s real click tree.  The proxy function's ``__name__`` is
reassigned per-leaf (``_run_{group}_{sub}``); object identity is not stable
across Typer's ``get_command`` conversion.
"""


@dataclass(frozen=True)
class DjangoGroup:
    """One overlay sub-app group description.

    ``core_dispatch`` flags groups whose subcommands live in
    ``teatree.core.management.commands`` (not in any overlay-owned
    ``manage.py``). When ``True``, :meth:`OverlayAppBuilder._bridge_subcommand`
    dispatches via :func:`managepy_core` (``python -m teatree``) so the call
    never reaches an overlay's ``manage.py`` whose settings module may not
    register the command (#1318 follow-up to #1312).
    """

    help_text: str
    subcommands: list[tuple[str, str]]
    core_dispatch: bool = False


DJANGO_GROUPS: dict[str, DjangoGroup] = {
    "worktree": DjangoGroup(
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
    "workspace": DjangoGroup(
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
    "run": DjangoGroup(
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
    "e2e": DjangoGroup(
        "E2E test commands.",
        [
            ("run", "Run E2E tests — dispatches to project or external runner based on overlay config."),
            ("trigger-ci", "Trigger E2E tests on a remote CI pipeline."),
            ("external", "Run Playwright tests from the external test repo (T3_PRIVATE_TESTS)."),
            ("project", "Run E2E tests from the project's own test directory."),
            (
                "post-evidence",
                "Post structured E2E evidence on the ticket (validation-gated, idempotent on env+commit).",
            ),
        ],
    ),
    "db": DjangoGroup(
        "Database operations.",
        [
            ("refresh", "Re-import the worktree database from dump/DSLR."),
            ("restore-ci", "Restore database from the latest CI dump."),
            ("reset-passwords", "Reset all user passwords to a known dev value."),
            ("query", "Run a read-only SQL query against the control DB; emit rows as JSON."),
            ("shell", "Drop into a Django shell against the resolved (gate) control DB."),
        ],
    ),
    "pr": DjangoGroup(
        "Pull request helpers.",
        [
            ("create", "Create a pull request for the ticket's branch."),
            ("ensure-pr", "Create a PR for an orphan branch (idempotent)."),
            ("check-gates", "Check whether session gates allow a phase transition."),
            ("fetch-issue", "Fetch issue details from the configured tracker."),
            ("detect-tenant", "Detect the current tenant variant from the overlay."),
            ("post-evidence", "Post test evidence as a PR comment."),
            ("sweep", "List your open PRs across the forge for the /t3:sweeping-prs skill."),
        ],
    ),
    "tasks": DjangoGroup(
        "Async task queue.",
        [
            ("cancel", "Cancel a task by ID."),
            ("claim", "Claim the next available task."),
            ("complete", "Mark a claimed task COMPLETED for work finished out-of-band."),
            ("create", "Enqueue the next-phase task for a ticket."),
            ("list", "List tasks with optional filters."),
            ("start", "Claim and run the next interactive task in the current terminal."),
            ("work-next-sdk", "Claim and execute an headless task."),
            ("work-next-user-input", "Claim and execute a user input task."),
        ],
    ),
    "followup": DjangoGroup(
        "Follow-up snapshots.",
        [
            ("refresh", "Return counts of tickets and tasks."),
            ("sync", "Synchronize followup data from MRs."),
            ("discover-mrs", "List the user's open non-draft PRs/MRs awaiting a review request."),
            ("remind", "Return list of pending user input tasks."),
        ],
        core_dispatch=True,
    ),
    "standup": DjangoGroup(
        "Auto-generated daily update (read-only).",
        [
            ("generate", "Generate a standup from transition + attempt data (read-only)."),
            ("stale", "List tickets with no activity past the staleness threshold (read-only)."),
        ],
    ),
    "lifecycle": DjangoGroup(
        "Session lifecycle and phase tracking.",
        [
            ("visit-phase", "Mark a phase as visited on the ticket's latest session."),
            ("clear-ledger", "Clear a reused ticket's stale phase ledger (sanctioned session-retire)."),
        ],
    ),
    "env": DjangoGroup(
        "Inspect and mutate the worktree env cache.",
        [
            ("show", "Print the env cache as the DB would render it."),
            ("set-var", "Persist an override on the worktree and refresh the cache."),
            ("unset", "Delete an override row and refresh the cache."),
            ("overrides", "List user-declared overrides for this worktree."),
            ("check", "Exit non-zero if the on-disk cache diverges from the DB render."),
            ("migrate-secrets", "Move POSTGRES_PASSWORD literals out of .t3-env.cache into pass."),
        ],
    ),
    "ticket": DjangoGroup(
        "Ticket state management.",
        [
            ("transition", "Transition a ticket to a new state."),
            ("clear", "Issue a per-diff CLEAR — the orchestrator's only merge output (BLUEPRINT §17.4.2)."),
            ("merge", "Execute the IN_REVIEW → MERGED keystone transition (BLUEPRINT §17.4)."),
            ("list", "List tickets, optionally filtered by state and/or overlay."),
            ("sync-completions", "Check post-ship tickets against upstream issues and advance completed ones."),
            ("comment", "Post a comment to an issue or work item by its URL."),
            ("context", "Durable per-ticket knowledge store: show / add / edit (#627)."),
        ],
    ),
    "availability": DjangoGroup(
        "24/7 dual question-mode (#58, BLUEPRINT §17.1 invariant 9).",
        [
            ("away", "Set manual away-mode override (questions queue as DeferredQuestion rows)."),
            ("present", "Set manual present-mode override (questions ask interactively)."),
            ("auto", "Clear manual override and fall back to schedule/default."),
            ("show", "Print the currently resolved mode and source (override/schedule/default)."),
        ],
    ),
    "questions": DjangoGroup(
        "Manage the away-mode deferred-question backlog (#58).",
        [
            ("record", "Record a deferred question (used by the PreToolUse away-mode hook)."),
            ("list", "List pending deferred questions, oldest first."),
            ("answer", "Resolve a pending question with a user answer."),
            ("dismiss", "Dismiss a pending question without answering it."),
        ],
    ),
    "pending_chat": DjangoGroup(
        "Manage the inbound Slack-DM queue (#1063).",
        [
            ("list", "List inbound rows from the last hour (or --all)."),
            ("mark-answered", "Stamp ``answered_at`` on rows matching a Slack ts."),
        ],
    ),
    "notify": DjangoGroup(
        "Bot→user Slack DM from the shell (#1030).",
        [
            ("send", "DM the user; exit 0 on delivery, 1 otherwise (sub-agent direct notify)."),
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


def _run_workers(project_path: Path, overlay_name: str, count: int, interval: float) -> None:
    """Spawn *count* ``db_worker`` subprocesses and block until they exit."""
    manage_py = str(project_path / "manage.py")
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    if overlay_name:
        env["T3_OVERLAY_NAME"] = overlay_name
    processes = [
        spawn(
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
        cmd = uv_cmd(project_path, "python", "manage.py", *args)
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

        for group_name, dj_group in DJANGO_GROUPS.items():
            group = typer.Typer(no_args_is_help=True, help=dj_group.help_text)
            for sub_name, sub_help in dj_group.subcommands:
                self._bridge_subcommand(group, group_name, sub_name, sub_help, core_dispatch=dj_group.core_dispatch)
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
