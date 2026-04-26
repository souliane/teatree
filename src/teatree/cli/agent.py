"""``t3 agent`` — launch Claude Code with auto-detected project context."""

import logging
import os
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

AGENT_PHASE_OPTION = typer.Option("", "--phase", help="Explicit TeaTree phase override.")
AGENT_SKILL_OPTION = typer.Option(
    None,
    "--skill",
    help="Explicit skill override. Repeat to load multiple skills.",
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

    from teatree.cli.doctor import IntrospectionHelpers  # noqa: PLC0415

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

    from teatree.config import load_config  # noqa: PLC0415

    context = "\n".join(context_lines)
    cmd = [claude_bin]
    if load_config().user.claude_chrome:
        cmd.append("--chrome")
    cmd.extend(["--append-system-prompt", context])

    if os.environ.get("T3_CONTRIBUTE", "").lower() == "true":
        from teatree import find_project_root  # noqa: PLC0415

        teatree_root = find_project_root()
        if teatree_root:
            cmd.extend(["--plugin-dir", str(teatree_root)])

    if task:
        cmd.extend(["-p", task])

    typer.echo(f"Launching Claude Code in {project_root}...")
    os.execvp(claude_bin, cmd)  # noqa: S606


def agent(
    task: str = typer.Argument("", help="What to work on (e.g. 'fix the sync bug', 'add a new command')"),
    phase: str = AGENT_PHASE_OPTION,
    skill: list[str] = AGENT_SKILL_OPTION,
) -> None:
    """Launch Claude Code with auto-detected project context."""
    from teatree.cli import _find_project_root  # noqa: PLC0415
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
