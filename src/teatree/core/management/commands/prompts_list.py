"""``manage.py prompts_list`` — list reusable :class:`Prompt` rows (#2513).

Backs the read-only ``t3 prompts list`` (the ``/prompts`` trigger's discovery
surface). Reads :class:`teatree.core.models.Prompt` rows and prints each prompt's
name, declared params, version depth, and description. ORM access lives in a
management command (the project's "anything touching the ORM is a management
command" rule). Strictly read-only — never mutates a row.
"""

from typing import IO, Annotated, Any, cast

import typer
from django_typer.management import TyperCommand

from teatree.core.machine_output import emit
from teatree.core.models import Prompt


def _line(prompt: Prompt) -> str:
    params = ",".join(prompt.params) if prompt.params else "—"
    return f"  {prompt.name:<24} params [{params:<20}] v{prompt.current_version}  {prompt.description}"


def _payload(prompt: Prompt) -> dict[str, Any]:
    return {
        "name": prompt.name,
        "body": prompt.body,
        "params": list(prompt.params or []),
        "description": prompt.description,
        "overlay": prompt.overlay,
        "version": prompt.current_version,
    }


class Command(TyperCommand):
    help = "List reusable prompts: name, params, version, description (read-only; #2513)."

    def handle(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the prompts as JSON.")] = False,
    ) -> None:
        prompts = list(Prompt.objects.all())
        human = "\n".join(["prompts:", *(_line(prompt) for prompt in prompts)])
        emit(
            {"prompts": [_payload(p) for p in prompts]},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=human,
        )
