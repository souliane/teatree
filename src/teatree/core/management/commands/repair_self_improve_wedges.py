"""Preview or repair legacy self-improve planning wedges through the ticket FSM."""

from typing import IO, Annotated, cast

import typer
from django_typer.management import command

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.models.self_improve_wedge_repair import repair_legacy_self_improve_wedges


class Command(MachineOutputCommand):
    """Preview or repair legacy self-improve planning wedges."""

    @command()
    def repair(
        self,
        *,
        apply: Annotated[
            bool, typer.Option("--apply", help="Advance eligible tickets and dismiss their wedges.")
        ] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON on stdout.")] = False,
    ) -> dict[str, list[int]]:
        payload = repair_legacy_self_improve_wedges(apply=apply)
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=(
                f"repairable={len(payload['repairable'])} repaired={len(payload['repaired'])} "
                f"missing_plan={len(payload['missing_plan'])} invalid_plan={len(payload['invalid_plan'])}"
            ),
        )
        return payload
