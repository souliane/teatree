"""``t3 agent`` — launch the configured agent with project context."""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import typer

from teatree.utils.django_bootstrap import ensure_django

logger = logging.getLogger(__name__)

AGENT_PHASE_OPTION = typer.Option("", "--phase", help="Explicit TeaTree phase override.")
AGENT_SKILL_OPTION = typer.Option(
    None,
    "--skill",
    help="Explicit skill override. Repeat to load multiple skills.",
)


@dataclass(frozen=True, slots=True)
class AgentLaunchContext:
    task: str
    project_root: Path
    context_lines: list[str]
    skills: list[str]
    ask_user_which_skill: bool


def _detect_agent_ticket_status(project_root: Path) -> str:
    if not (project_root / "manage.py").is_file():
        return ""
    try:
        ensure_django()
        from teatree.core.intake.resolve import resolve_worktree  # noqa: PLC0415 — deferred: keeps CLI startup light

        return str(resolve_worktree().ticket.state)
    except Exception:
        logger.debug("Failed to detect agent ticket status", exc_info=True)
        return "(error)"


def _configured_cli_runtime(*, task: str) -> str:
    """Return the attended or unattended CLI runtime selected by the project."""
    from django.conf import settings  # noqa: PLC0415 — Django is bootstrapped by the command

    setting_name = "TEATREE_HEADLESS_RUNTIME" if task else "TEATREE_INTERACTIVE_RUNTIME"
    return str(getattr(settings, setting_name, "claude-code"))


def _build_agent_context(
    *,
    task: str,
    context_lines: list[str],
    skills: list[str],
    ask_user_which_skill: bool,
    skill_prefix: str,
) -> str:
    """Build the shared startup context in the target runtime's skill syntax."""
    from teatree.cli.doctor import IntrospectionHelpers  # noqa: PLC0415 — deferred: keeps CLI startup light

    lines = list(context_lines)
    teatree_editable, teatree_url = IntrospectionHelpers.editable_info("teatree")
    if teatree_editable and teatree_url:
        lines.append(f"TeaTree source (editable): {teatree_url.removeprefix('file://')}")
    lines.append("")
    if skills:
        lines.extend(
            (
                "Load only these skills before starting work:",
                *(f"  - {skill_prefix}{skill}" for skill in skills),
            ),
        )
    if ask_user_which_skill:
        lines.extend(
            (
                "TeaTree could not infer the lifecycle skill for this session.",
                "Before doing any work, ask the user which lifecycle skill to load.",
            ),
        )
    lines.extend(("", "Run `t3 --help` to see available commands.", "Run `uv run pytest` to run tests."))
    if task:
        lines.extend(("", f"Task: {task}"))
    return "\n".join(lines)


def _launch_claude(
    *,
    task: str,
    project_root: Path,
    context_lines: list[str],
    skills: list[str],
    ask_user_which_skill: bool,
) -> None:
    """Shared logic: resolve skills, build prompt, exec into claude."""
    import shutil  # noqa: PLC0415 — deferred: loaded only when this command runs

    claude_bin = shutil.which("claude")
    if not claude_bin:
        typer.echo("claude CLI not found on PATH. Install Claude Code first.")
        raise typer.Exit(code=1)

    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light

    settings = get_effective_settings()
    context = _build_agent_context(
        task=task,
        context_lines=context_lines,
        skills=skills,
        ask_user_which_skill=ask_user_which_skill,
        skill_prefix="/",
    )
    cmd = [claude_bin]
    if settings.claude_chrome:
        cmd.append("--chrome")
    cmd.extend(["--append-system-prompt", context])

    if settings.contribute_plugin_dir:
        from teatree import find_project_root  # noqa: PLC0415 — deferred: keeps CLI startup light

        teatree_root = find_project_root()
        if teatree_root:
            cmd.extend(["--plugin-dir", str(teatree_root)])

    if task:
        # `-p` turns this exec into a headless print-mode run: the operator typed the
        # command, but nobody is present for the run itself. So this branch — unlike
        # the interactive one below it — pins the unattended mode and takes the same
        # base-URL guard as every other seam that spawns a child no human can watch.
        from teatree.agents import permission_modes  # noqa: PLC0415 — deferred: keeps CLI startup light
        from teatree.llm.credentials import (  # noqa: PLC0415 — deferred: keeps CLI startup light
            reject_ambient_base_url_redirect,
        )

        reject_ambient_base_url_redirect()
        cmd.extend(["-p", task, "--permission-mode", permission_modes.UNATTENDED])

    typer.echo(f"Launching Claude Code in {project_root}...")
    os.execvp(claude_bin, cmd)  # noqa: S606 — argv list, no shell


def _launch_codex(
    *,
    task: str,
    project_root: Path,
    context_lines: list[str],
    skills: list[str],
    ask_user_which_skill: bool,
) -> None:
    """Build native Codex CLI argv and replace the current process."""
    import shutil  # noqa: PLC0415 — deferred: loaded only when this command runs

    codex_bin = shutil.which("codex")
    if not codex_bin:
        typer.echo("codex CLI not found on PATH. Install Codex CLI first.")
        raise typer.Exit(code=1)

    context = _build_agent_context(
        task=task,
        context_lines=context_lines,
        skills=skills,
        ask_user_which_skill=ask_user_which_skill,
        skill_prefix="$",
    )
    context_config = f"developer_instructions={json.dumps(context)}"
    if task:
        cmd = [
            codex_bin,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(project_root),
            "-c",
            context_config,
            task,
        ]
    else:
        cmd = [codex_bin, "-C", str(project_root), "-c", context_config]

    typer.echo(f"Launching Codex in {project_root}...")
    os.execvp(codex_bin, cmd)  # noqa: S606 — argv list, no shell


def _launch_agent(*, runtime: str, launch: AgentLaunchContext) -> None:
    """Dispatch the manual CLI session without touching the headless Harness seam."""
    launchers = {
        "claude": _launch_claude,
        "claude-code": _launch_claude,
        "codex": _launch_codex,
    }
    try:
        launcher = launchers[runtime]
    except KeyError as exc:
        msg = f"Unsupported agent runtime: {runtime}"
        raise typer.BadParameter(msg) from exc
    launcher(
        task=launch.task,
        project_root=launch.project_root,
        context_lines=launch.context_lines,
        skills=launch.skills,
        ask_user_which_skill=launch.ask_user_which_skill,
    )


def agent(
    task: str = typer.Argument("", help="What to work on (e.g. 'fix the sync bug', 'add a new command')"),
    phase: str = AGENT_PHASE_OPTION,
    skill: list[str] = AGENT_SKILL_OPTION,
) -> None:
    """Launch the configured agent with auto-detected project context."""
    ensure_django()

    from teatree.cli import _find_project_root  # noqa: PLC0415 — deferred: breaks agent ↔ cli cycle
    from teatree.config import discover_active_overlay  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.skill_support.loading import SkillLoadingPolicy  # noqa: PLC0415 — deferred: keeps CLI startup light

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
            ticket_status=_detect_agent_ticket_status(project_root) if active else "",
            explicit_phase=phase,
            explicit_skills=skill or [],
            overlay_active=bool(active),
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    _launch_agent(
        runtime=_configured_cli_runtime(task=task),
        launch=AgentLaunchContext(
            task=task,
            project_root=project_root,
            context_lines=lines,
            skills=selection.skills,
            ask_user_which_skill=selection.ask_user,
        ),
    )
