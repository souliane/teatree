"""``t3 loop directives`` — read the standing directives, and switch a slot off (#4166).

Split out of ``cli.loop.app`` (module-health), mirroring ``cli.loop.listing``. Every
verb delegates to a management command (anything touching the ORM is a management
command, not a plain typer command): ``show`` to ``loop_directives``, ``disable`` /
``enable`` to ``loop_directive_set``.

``disable --all`` is the whole-feature kill switch. It is a CLI verb rather than a
config setting on purpose: the hazard is one expensive slot, and a whole-feature
boolean would force a choice between the runaway and losing the safety rule that
costs nothing.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

directives_app = typer.Typer(
    name="directives",
    help="Read the standing directives, or switch a slot off and back on (#4166).",
    no_args_is_help=True,
)


def _call(command: str, *args: str, **kwargs: object) -> None:
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    call_command(command, *args, **kwargs)


@directives_app.command("show")
def show_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the standing directives as JSON."),
) -> None:
    """Print the standing directives, their scope, their delivery cost and the turn budget.

    Read-only. The ``--json`` payload — ``{slot_id, cadence_seconds, text, scope,
    wakes_session}`` per directive — is the harness-neutral contract: a non-Claude
    harness reads it and writes only its own delivery adapter.
    """
    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    _call("loop_directives", **kwargs)


@directives_app.command("disable")
def disable_command(
    slot_ids: list[str] | None = typer.Argument(None, help="Slot ids to switch off."),
    *,
    all_slots: bool = typer.Option(False, "--all", help="Switch every slot off."),
) -> None:
    """Switch each named slot off by writing an empty, versioned override body."""
    _call("loop_directive_set", "disable", *(slot_ids or []), all_slots=all_slots)


@directives_app.command("enable")
def enable_command(
    slot_ids: list[str] | None = typer.Argument(None, help="Slot ids to switch back on."),
    *,
    all_slots: bool = typer.Option(False, "--all", help="Switch every slot back on."),
) -> None:
    """Switch each named slot back on, restoring the owner's own text where there is one."""
    _call("loop_directive_set", "enable", *(slot_ids or []), all_slots=all_slots)


__all__ = ["directives_app", "disable_command", "enable_command", "show_command"]
