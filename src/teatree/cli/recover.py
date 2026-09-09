"""Top-level ``t3 recover`` — find and recover work stranded by an outage (#1764).

Thin Typer wrapper forwarding to the active overlay's ``manage.py recover``
(the django-typer command in ``teatree.core.management.commands.recover``),
mirroring the ``t3 task`` alias. The active overlay is resolved the same way as
the rest of the CLI; with no overlay registered it falls back to teatree's own
management command via ``python -m teatree``. Default is a dry-run report;
``--requeue`` reopens FAILED tasks, bounded by ``--since`` and ``--max`` (#4710).
There is no ``--snapshot`` — stranded work is surfaced for salvage (push to a PR),
not auto-captured.
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
    since: Annotated[
        str,
        typer.Option("--since", help="Only reopen tasks that failed within this window (default: 1d)."),
    ] = "",
    max_reopen: Annotated[
        int,
        typer.Option("--max", help="Refuse to reopen more than this many tasks (default: 25)."),
    ] = -1,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the structured report as JSON."),
    ] = False,
    overlay: Annotated[
        str,
        typer.Option("--overlay", help="Which overlay's manage.py runs the report (default: active overlay)."),
    ] = "",
) -> None:
    """Forward `t3 recover [--requeue] [--since D] [--max N] [--json]` to `t3 <overlay> recover`.

    The flags are declared explicitly (not a raw ``ctx.args`` passthrough) so
    Typer's group parser does not mis-read a leading ``--requeue`` as a
    subcommand name (`No such command '--requeue'`). ``--requeue`` reopens FAILED
    tasks, ``--since`` and ``--max`` bound that reopen, and ``--json`` emits the
    structured report; all forward to the management command for parity, and the default
    is the dry-run report. The bound defaults are forwarded only when the caller sets
    them, so the management command stays their single source of truth.
    """
    project_path, overlay_name = _resolve_overlay()
    forwarded: list[str] = []
    if requeue:
        forwarded.append("--requeue")
    if since:
        forwarded += ["--since", since]
    # `>= 0`, not truthiness: `--max 0` (refuse every reopen unconditionally) is a real ask.
    if max_reopen >= 0:
        forwarded += ["--max", str(max_reopen)]
    if json_output:
        forwarded.append("--json")
    managepy(project_path, "recover", *forwarded, overlay_name=overlay or overlay_name)
