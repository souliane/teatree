"""Top-level ``t3 recover`` — find and recover work stranded by an outage (#1764).

Thin Typer wrapper forwarding to the active overlay's ``manage.py recover``
(the django-typer command in ``teatree.core.management.commands.recover``),
mirroring the ``t3 task`` alias. The active overlay is resolved the same way as
the rest of the CLI; with no overlay registered it falls back to teatree's own
management command via ``python -m teatree``. Default is a dry-run report;
``--requeue`` reopens FAILED tasks. There is no ``--snapshot`` — stranded work is
surfaced for salvage (push to a PR), not auto-captured.
"""

from pathlib import Path

import typer

from teatree.cli.overlay import managepy

recover_app = typer.Typer(
    name="recover",
    no_args_is_help=False,
    help="Find (and optionally recover) work stranded by a network-outage death (#1764).",
    context_settings={
        "allow_extra_args": True,
        "allow_interspersed_args": False,
        "ignore_unknown_options": True,
    },
)


def _resolve_overlay() -> tuple[Path | None, str]:
    from teatree.config import discover_active_overlay  # noqa: PLC0415

    active = discover_active_overlay()
    if active is None:
        return None, ""
    return active.project_path, active.name


def _split_overlay_flag(args: list[str]) -> tuple[str, list[str]]:
    """Pull a leading-or-anywhere ``--overlay <name>`` / ``--overlay=<name>`` out of *args*.

    Returns ``(overlay_name, remaining_args)``. ``--overlay`` selects which
    overlay's ``manage.py`` runs the report; the rest forward unchanged.
    """
    overlay = ""
    rest: list[str] = []
    it = iter(args)
    for arg in it:
        if arg == "--overlay":
            overlay = next(it, "")
        elif arg.startswith("--overlay="):
            overlay = arg.split("=", 1)[1]
        else:
            rest.append(arg)
    return overlay, rest


@recover_app.callback(
    invoke_without_command=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def recover(ctx: typer.Context) -> None:
    """Forward `t3 recover [flags]` to `t3 <overlay> recover`."""
    overlay_override, forwarded = _split_overlay_flag(ctx.args)
    project_path, overlay_name = _resolve_overlay()
    managepy(project_path, "recover", *forwarded, overlay_name=overlay_override or overlay_name)
