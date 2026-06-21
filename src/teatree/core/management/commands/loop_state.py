"""``manage.py loop_state`` — pause/resume/disable/enable a mini-loop (#1913).

Backs ``t3 loop {pause,resume,disable,enable,status} <name>``. ORM access lives
here (a management command, not a plain typer command) per the project's
"anything touching the ORM is a management command" rule.

The ``enable``/``disable``/``resume`` verbs move TWO planes in lock-step inside
one transaction: the durable ``LoopState`` control tier (#1913) AND the
row-level ``Loop.enabled`` column that the #2584 master tick reads as its source
of truth (``not row.enabled`` skips a loop). Before this, ``enable`` wrote only
``LoopState`` and left ``Loop.enabled`` stale, so the verb reported "now enabled"
while the master never ticked the loop. ``pause`` is the reversible control-plane
hold only — it does NOT flip the durable ``Loop.enabled`` row (a paused loop
returns to running with ``resume`` without re-enabling a row that may have been
deliberately ``disable``d).

Each transition is the atomic, idempotent ``LoopState`` upsert paired with the
idempotent ``Loop.enabled`` update; the command re-reads and reports the LANDED
status so the operator sees the verified state rather than an echo of the request.
"""

import json
from typing import Annotated

import typer
from django.db import transaction
from django_typer.management import TyperCommand, command

from teatree.core.models import Loop, LoopState


def _report(name: str, *, json_output: bool, stdout_write) -> None:  # noqa: ANN001
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
        LoopState.objects.pause(name)
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="resume")
    def resume(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Return *name* to ENABLED, clearing a pause OR a disable — both planes."""
        with transaction.atomic():
            LoopState.objects.resume(name)
            Loop.objects.set_enabled(name, enabled=True)
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="disable")
    def disable(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Move *name* into the durable DISABLED kill-switch — both planes."""
        with transaction.atomic():
            LoopState.objects.disable(name)
            Loop.objects.set_enabled(name, enabled=False)
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="enable")
    def enable(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Return *name* to ENABLED (alias of resume) — both planes."""
        with transaction.atomic():
            LoopState.objects.enable(name)
            Loop.objects.set_enabled(name, enabled=True)
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
