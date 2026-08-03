"""``t3 loops`` — DB-configured autonomous loops (#1796).

``t3 loops list`` prints the loops from the DB (read-only). ``t3 loops audit`` answers the
question the DB alone cannot (#3842): which shipped loops, presets and schedules are
missing, disabled, not ticking, or running a value that no longer matches the one
``defaults.toml`` ships (#4096) — sourced from the shipped seed tables, so a deleted row
is visible at all. ``t3 loops delete`` is the audited removal seam its typed confirm gates.
``t3 loops tick --loop <name>`` runs ONE enabled, due loop — the per-loop primitive the
self-rescheduling loop-timer chain drives (:mod:`teatree.loops.timer_chains`, the
sole driver since PR-28 retired the native-``/loop`` cron mirror). There is NO
master tick: ``t3 loops tick`` with no ``--loop`` is a hard error. Per-loop
management — add / edit / enable / disable —
is via ``t3 loop enable`` / ``t3 loop disable`` + the Django admin (``Loop`` rows: name /
prompt / delay / enabled). ORM access lives in the ``loops_tick`` / ``loops_list``
management commands, not a plain typer command.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

loops_app = typer.Typer(
    name="loops",
    no_args_is_help=True,
    help="Manage DB-configured autonomous loops (#1796).",
)


@loops_app.callback()
def _loops() -> None:
    """Keep ``loops`` a command group (one subcommand would otherwise collapse to single-command)."""


@loops_app.command("list")
def list_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the loops as JSON."),
) -> None:
    """List DB-configured autonomous loops: name, enabled, delay, last run, next due.

    Read-only: it reads the ``Loop`` table and prints it — never ticks, marks a
    run, or mutates a row.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    call_command("loops_list", **kwargs)


@loops_app.command("audit")
def audit_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the findings as JSON."),
) -> None:
    """Report every shipped loop/preset/schedule missing, disabled, not ticking, or diverged.

    Sources the expected set from the shipped seed tables rather than the DB, so a row
    somebody deleted is visible at all. Exits NON-ZERO when any finding is a fault; a
    deliberate operator choice (a shipped-off loop, an inactive calendar, a mask or calendar
    edited away from what ships) is a note, reported and never rewritten.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    kwargs: dict[str, bool] = {"json_output": True} if json_output else {}
    try:
        call_command("shipped_seed", "audit", **kwargs)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


@loops_app.command("delete")
def delete_command(
    name: str = typer.Argument(..., help="Loop to delete."),
    *,
    confirm: str = typer.Option("", "--confirm", help="Typed phrase `stop-<name>`; required for a shipped loop."),
    json_output: bool = typer.Option(False, "--json", help="Emit the result as JSON."),
) -> None:
    """Delete a loop row — a loop that ships by default needs ``--confirm stop-<name>``.

    Soft protection, not prohibition: the phrase names what stops happening, and the
    refusal quotes the shipped description so an unclear operator learns rather than
    just being blocked. `t3 setup` recreates a deleted shipped loop.
    """
    _delegate_delete("delete-loop", name, confirm=confirm, json_output=json_output)


def _delegate_delete(subcommand: str, name: str, *, confirm: str, json_output: bool) -> None:
    """Call ``shipped_seed <subcommand>``; map its ``SystemExit`` onto the typer exit code."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    args = ["shipped_seed", subcommand, name]
    kwargs: dict[str, str | bool] = {}
    if confirm:
        kwargs["confirm"] = confirm
    if json_output:
        kwargs["json_output"] = True
    try:
        call_command(*args, **kwargs)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


@loops_app.command("tick")
def tick_command(
    *,
    loop: str = typer.Option(
        "",
        "--loop",
        help=(
            "REQUIRED. Run ONE enabled, due DB Loop by name — what the self-rescheduling loop-timer "
            "chain drives, claiming the per-loop `loop:<name>` lease. There is no master tick: omitting "
            "--loop is a hard error."
        ),
    ),
    overlay: str = typer.Option("", "--overlay", help="Restrict scanning to the named overlay (default: all)."),
    json_output: bool = typer.Option(False, "--json", help="Emit the tick report as JSON."),
) -> None:
    """Run ONE enabled, due loop by name — the per-loop primitive the loop-timer chain drives.

    Scopes the tick to that single enabled, due ``Loop`` row, claiming the disjoint
    per-loop ``loop:<name>`` lease so the per-loop loops run in parallel. **There is
    no master tick:** omitting ``--loop`` is a hard error (the ``loops_tick``
    management command refuses it). Delegates to that management command.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    kwargs: dict[str, str | bool] = {}
    if loop:
        kwargs["loop"] = loop
    if overlay:
        kwargs["overlay"] = overlay
    if json_output:
        kwargs["json_output"] = True
    call_command("loops_tick", **kwargs)


__all__ = ["loops_app"]
