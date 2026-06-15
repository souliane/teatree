"""``manage.py loop_state`` — pause/resume/disable/enable a mini-loop (#1913).

Backs ``t3 loop pause/resume/disable/enable/status <name>``. ORM access lives
here (a management command, not a plain typer command) per the project's
"anything touching the ORM is a management command" rule.

Each transition is the atomic, idempotent ``LoopState`` upsert; the command
re-reads and reports the LANDED status so the operator sees the verified state
rather than an echo of the request.
"""

import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command


def _report(name: str, *, json_output: bool, stdout_write) -> None:  # noqa: ANN001
    from teatree.core.models import LoopState  # noqa: PLC0415

    status = LoopState.objects.status_of(name)
    if json_output:
        stdout_write(json.dumps({"name": name, "status": status.value}, indent=2))
    else:
        stdout_write(f"OK    loop {name!r} is now {status.value}.")


class Command(TyperCommand):
    help = "Pause, resume, disable, enable, or inspect a mini-loop's durable state (#1913)."

    @command(name="pause")
    def pause(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name (e.g. review, ship, dispatch).")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Move *name* into the reversible PAUSED hold."""
        from teatree.core.models import LoopState  # noqa: PLC0415

        LoopState.objects.pause(name)
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="resume")
    def resume(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Return *name* to ENABLED, clearing a pause OR a disable."""
        from teatree.core.models import LoopState  # noqa: PLC0415

        LoopState.objects.resume(name)
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="disable")
    def disable(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Move *name* into the durable DISABLED kill-switch."""
        from teatree.core.models import LoopState  # noqa: PLC0415

        LoopState.objects.disable(name)
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="enable")
    def enable(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Return *name* to ENABLED (alias of resume)."""
        from teatree.core.models import LoopState  # noqa: PLC0415

        LoopState.objects.enable(name)
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="status")
    def status(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Report *name*'s durable state (ENABLED when no row exists)."""
        _report(name, json_output=json_output, stdout_write=self.stdout.write)
