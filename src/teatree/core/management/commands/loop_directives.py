"""``manage.py loop_directives`` — the standing directives, resolved (#4166 Phase 1).

Backs the read-only ``t3 loop directives``. This is the harness-neutral read
surface of :mod:`teatree.loop.standing_directives`: any harness builds its own
delivery adapter from the ``{slot_id, cadence_seconds, text, scope}`` payload
``--json`` prints, with no teatree change. ORM access (the ``Prompt``-row text
override) lives in a management command, per the project rule. Read-only.
"""

from typing import IO, Annotated, Any, cast

import typer

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.loop.standing_directives import ResolvedDirective, resolve_standing_directives


def _line(directive: ResolvedDirective) -> str:
    return f"  {directive.slot_id:<26} every {directive.cadence_seconds}s  [{directive.scope}]\n    {directive.text}"


class Command(MachineOutputCommand):
    help = "Print the standing directives with their resolved cadence and text (read-only; #4166)."

    def handle(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the directives as JSON.")] = False,
    ) -> dict[str, Any]:
        directives = resolve_standing_directives()
        payload = {"directives": [d.as_dict() for d in directives]}
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(["standing directives:", *(_line(d) for d in directives)]),
        )
        return payload
