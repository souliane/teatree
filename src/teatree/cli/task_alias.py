"""Top-level ``t3 task`` alias for ``t3 <overlay> tasks <sub>`` (#1306).

Sub-agent prompts and skill briefs reference the short form
``t3 task complete <id>`` for marking a task done, but the actual
implementation lives under the overlay-scoped tasks group (e.g.
``t3 teatree tasks complete <id>``). Without the alias the short form
errored with ``No such command 'task'.`` and broke copy-pasted prompts.

This module exposes a thin Typer group that forwards every argument to
the active overlay's ``manage.py tasks <sub> ...`` so the existing
implementation owns the behaviour. The active overlay is resolved the
same way as the rest of the CLI (``discover_active_overlay``); when no
overlay is registered the alias falls back to teatree's own management
command via ``python -m teatree``.
"""

from pathlib import Path

import typer

from teatree.cli.overlay import managepy

task_app = typer.Typer(
    name="task",
    no_args_is_help=True,
    help="Alias for `t3 <overlay> tasks <sub>` (sub-agent-friendly short form, #1306).",
    context_settings={
        "allow_extra_args": True,
        "allow_interspersed_args": False,
        "ignore_unknown_options": True,
    },
)


def _resolve_overlay() -> tuple[Path | None, str]:
    """Return (project_path, overlay_name) for the active overlay, if any."""
    from teatree.config import discover_active_overlay  # noqa: PLC0415 — deferred: keeps CLI startup light

    active = discover_active_overlay()
    if active is None:
        return None, ""
    return active.project_path, active.name


@task_app.command(
    name="complete",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def _complete(ctx: typer.Context) -> None:
    """Forward `t3 task complete <id> [flags]` to `t3 <overlay> tasks complete`."""
    project_path, overlay_name = _resolve_overlay()
    managepy(project_path, "tasks", "complete", *ctx.args, overlay_name=overlay_name)


@task_app.command(
    name="list",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def _list(ctx: typer.Context) -> None:
    """Forward `t3 task list [flags]` to `t3 <overlay> tasks list`."""
    project_path, overlay_name = _resolve_overlay()
    managepy(project_path, "tasks", "list", *ctx.args, overlay_name=overlay_name)


@task_app.command(
    name="cancel",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def _cancel(ctx: typer.Context) -> None:
    """Forward `t3 task cancel <id> [flags]` to `t3 <overlay> tasks cancel`."""
    project_path, overlay_name = _resolve_overlay()
    managepy(project_path, "tasks", "cancel", *ctx.args, overlay_name=overlay_name)
