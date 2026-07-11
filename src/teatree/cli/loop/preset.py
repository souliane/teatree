"""``t3 loop preset {list,show,use,auto,create,edit,delete}`` — the preset control surface (#3159).

Thin typer verbs that delegate to the ``loop_preset`` Django management command
(the ``cli.loop.state`` pattern — anything touching the ORM is a management
command, not a plain typer command). :func:`register` attaches the ``preset``
subgroup onto the shared ``loop_app``.
"""

from dataclasses import dataclass
from typing import Annotated

import typer

from teatree.utils.django_bootstrap import ensure_django


def _delegate(*args: str, json_output: bool = False) -> None:
    ensure_django()
    from django.core.management import call_command  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    kwargs: dict[str, bool] = {"json_output": True} if json_output else {}
    try:
        call_command("loop_preset", *args, **kwargs)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


def register(loop_app: typer.Typer) -> None:
    """Attach the ``preset`` subgroup (list/show/use/auto/create/edit/delete) onto loop_app."""
    preset_app = typer.Typer(
        name="preset", no_args_is_help=True, help="Named loop-state presets — mode switching (#3159)."
    )

    @preset_app.command("list")
    def list_command(*, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
        """List every preset with its pin, scope, entry count, and ACTIVE marker."""
        _delegate("list", json_output=json_output)

    @preset_app.command("show")
    def show_command(
        name: Annotated[str, typer.Argument()] = "",
        *,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Show a preset, or (no arg) the active preset + WHY + per-loop verdict table."""
        args = ["show", name] if name else ["show"]
        _delegate(*args, json_output=json_output)

    @preset_app.command("use")
    def use_command(
        name: Annotated[str, typer.Argument()],
        *,
        for_: Annotated[str, typer.Option("--for", help="TTL like 2h/30m/1d.")] = "",
        until: Annotated[str, typer.Option("--until", help="Explicit ISO-8601 expiry.")] = "",
        hold: Annotated[bool, typer.Option("--hold", help="Sticky until cleared.")] = False,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Activate a preset as the manual override (default: until the next scheduled boundary)."""
        _delegate(*_use_args(name, for_=for_, until=until, hold=hold), json_output=json_output)

    @preset_app.command("auto")
    def auto_command(*, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
        """Clear the manual override so the active schedule decides again."""
        _delegate("auto", json_output=json_output)

    @preset_app.command("create")
    def create_command(
        name: Annotated[str, typer.Argument()],
        *,
        set_: Annotated[list[str], typer.Option("--set", help="<loop>=on|off (repeatable).")] = [],  # noqa: B006 — typer Option default — idiomatic mutable default for a repeatable flag
        description: Annotated[str, typer.Option("--description")] = "",
        pin: Annotated[str, typer.Option("--pin")] = "",
        scope: Annotated[str, typer.Option("--scope")] = "",
    ) -> None:
        """Create a preset from ``--set`` entries, an optional availability pin and overlay scope."""
        _delegate(*_edit_args("create", name, _EditFields(set_, description, pin, scope)))

    @preset_app.command("edit")
    def edit_command(
        name: Annotated[str, typer.Argument()],
        *,
        set_: Annotated[list[str], typer.Option("--set", help="<loop>=on|off|inherit (repeatable).")] = [],  # noqa: B006 — typer Option default — idiomatic mutable default for a repeatable flag
        description: Annotated[str, typer.Option("--description")] = "",
        pin: Annotated[str, typer.Option("--pin")] = "",
        scope: Annotated[str, typer.Option("--scope")] = "",
    ) -> None:
        """Edit a preset's entries / description / pin / scope in place."""
        _delegate(*_edit_args("edit", name, _EditFields(set_, description, pin, scope)))

    @preset_app.command("delete")
    def delete_command(
        name: Annotated[str, typer.Argument()],
        *,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Delete a preset (a slot/override still pointing at it fails open to base config)."""
        _delegate("delete", name, json_output=json_output)

    loop_app.add_typer(preset_app, name="preset")


def _use_args(name: str, *, for_: str, until: str, hold: bool) -> list[str]:
    args = ["use", name]
    if for_:
        args += ["--for", for_]
    if until:
        args += ["--until", until]
    if hold:
        args += ["--hold"]
    return args


@dataclass(frozen=True, slots=True)
class _EditFields:
    """The create/edit CLI flags (`--set`/`--description`/`--pin`/`--scope`) bundled for passthrough."""

    set_: list[str]
    description: str
    pin: str
    scope: str


def _edit_args(verb: str, name: str, fields: _EditFields) -> list[str]:
    args = [verb, name]
    for entry in fields.set_:
        args += ["--set", entry]
    if fields.description:
        args += ["--description", fields.description]
    if fields.pin:
        args += ["--pin", fields.pin]
    if fields.scope:
        args += ["--scope", fields.scope]
    return args


__all__ = ["register"]
