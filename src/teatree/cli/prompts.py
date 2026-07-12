"""``t3 prompts`` — reusable, triggerable prompts (#2513).

``t3 prompts list`` prints the prompts from the DB (read-only). ``t3 prompts render
<name> --arg k=v`` resolves a prompt to its rendered instruction — the ``/prompts``
trigger. Per-prompt authoring (body / params / version history) lives in the Django
admin. Delegates to the ``prompts_list`` / ``prompts_render`` management commands
(ORM access lives in a management command, not a plain typer command).
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

prompts_app = typer.Typer(
    name="prompts",
    no_args_is_help=True,
    help="Manage and trigger reusable prompts (#2513).",
)


@prompts_app.callback()
def _prompts() -> None:
    """Keep ``prompts`` a command group (one subcommand would otherwise collapse)."""


@prompts_app.command("list")
def list_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the prompts as JSON."),
) -> None:
    """List reusable prompts: name, declared params, version depth, description.

    Read-only: reads the ``Prompt`` table and prints it — never mutates a row.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    call_command("prompts_list", **kwargs)


@prompts_app.command("render")
def render_command(
    name: str = typer.Argument(..., help="The prompt name to render."),
    *,
    arg: list[str] = typer.Option(None, "--arg", help="A declared-param value as KEY=VALUE (repeatable)."),
) -> None:
    """Render a reusable prompt by name with its declared params (the ``/prompts`` trigger).

    Read-only: loads the row and renders it — never mutates. A missing/undeclared
    param or an unknown name is a loud error, never a silent wrong-render.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    call_command("prompts_render", name, arg=arg or [])


__all__ = ["prompts_app"]
