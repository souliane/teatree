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
from typing import Annotated

import typer

from teatree.cli.overlay import managepy

recover_app = typer.Typer(
    name="recover",
    no_args_is_help=False,
    help="Find (and optionally recover) work stranded by a network-outage death (#1764).",
)


def _resolve_overlay() -> tuple[Path | None, str]:
    from teatree.config import discover_active_overlay  # noqa: PLC0415 — deferred: keeps CLI startup light

    active = discover_active_overlay()
    if active is None:
        return None, ""
    return active.project_path, active.name


@recover_app.callback(invoke_without_command=True)
def recover(
    *,
    requeue: Annotated[
        bool,
        typer.Option("--requeue", help="Reopen genuinely-incomplete FAILED (incl. outage-death) tasks."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the structured report as JSON."),
    ] = False,
    overlay: Annotated[
        str,
        typer.Option("--overlay", help="Which overlay's manage.py runs the report (default: active overlay)."),
    ] = "",
) -> None:
    """Forward `t3 recover [--requeue] [--json]` to `t3 <overlay> recover`.

    The flags are declared explicitly (not a raw ``ctx.args`` passthrough) so
    Typer's group parser does not mis-read a leading ``--requeue`` as a
    subcommand name (`No such command '--requeue'`). ``--requeue`` reopens FAILED
    tasks and ``--json`` emits the structured report; both forward to the
    management command for parity, and the default is the dry-run report.
    """
    project_path, overlay_name = _resolve_overlay()
    forwarded: list[str] = []
    if requeue:
        forwarded.append("--requeue")
    if json_output:
        forwarded.append("--json")
    managepy(project_path, "recover", *forwarded, overlay_name=overlay or overlay_name)
