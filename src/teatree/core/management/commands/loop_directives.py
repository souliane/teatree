"""``manage.py loop_directives`` — the standing directives, resolved (#4166 Phase 1).

Backs the read-only ``t3 loop directives show``. This is the harness-neutral read
surface of :mod:`teatree.loop.standing_directives`: any harness builds its own
delivery adapter from the ``{slot_id, cadence_seconds, text, scope, wakes_session}``
payload ``--json`` prints, with no teatree change. ORM access (the ``Prompt``-row
text override) lives in a management command, per the project rule. Read-only.

The payload carries the self-woken turn budget alongside the directives, because
"what does this cost per hour" is the question the delivery shape exists to answer
and it should not have to be re-derived by every reader.
"""

from typing import IO, Annotated, Any, cast

import typer

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.loop.standing_directives import ResolvedDirective, resolve_standing_directives, self_woken_turns_per_hour


def _line(directive: ResolvedDirective) -> str:
    cost = "wakes the session" if directive.wakes_session else "no turn"
    return (
        f"  {directive.slot_id:<26} every {directive.cadence_seconds}s  "
        f"[{directive.scope}, {cost}]\n    {directive.text}"
    )


def _budget_line(budget: dict[str, int]) -> str:
    return (
        f"  self-woken turns/hour: {budget['per_session']} per attended session "
        f"+ {budget['per_host_singleton']} per host"
    )


class Command(MachineOutputCommand):
    help = "Print the standing directives with their resolved cadence, scope, cost and text (#4166)."

    def handle(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the directives as JSON.")] = False,
    ) -> dict[str, Any]:
        directives = resolve_standing_directives()
        budget = self_woken_turns_per_hour()
        payload = {"directives": [d.as_dict() for d in directives], "self_woken_turns_per_hour": budget}
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(["standing directives:", *(_line(d) for d in directives), _budget_line(budget)]),
        )
        return payload
