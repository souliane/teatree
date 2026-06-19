"""``manage.py prompts_render`` — resolve a prompt to its rendered instruction (#2513).

Backs the ``/prompts`` trigger: given a prompt name and ``--arg k=v`` pairs for its
declared params, prints the rendered body — the instruction the agent then acts on.
Read-only: it loads the row and renders it, never mutating. An unknown prompt name,
a missing declared param, or an undeclared arg is a loud :class:`CommandError`
rather than a silent wrong-render.
"""

from typing import Annotated

import typer
from django.core.management.base import CommandError
from django_typer.management import TyperCommand

from teatree.core.models import Prompt
from teatree.core.models.prompt import MissingPromptParamError, UnknownPromptParamError


def _parse_args(pairs: list[str]) -> dict[str, str]:
    """Parse ``k=v`` pairs into a kwargs dict; a malformed pair is a CommandError."""
    args: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            msg = f"--arg must be KEY=VALUE, got {pair!r}"
            raise CommandError(msg)
        args[key] = value
    return args


class Command(TyperCommand):
    help = "Render a reusable prompt by name with its declared params (read-only; #2513)."

    def handle(
        self,
        name: Annotated[str, typer.Argument(help="The prompt name to render.")],
        *,
        arg: Annotated[
            list[str] | None,
            typer.Option("--arg", help="A declared-param value as KEY=VALUE (repeatable)."),
        ] = None,
    ) -> None:
        prompt = Prompt.objects.by_name(name)
        if prompt is None:
            msg = f"no prompt named {name!r}"
            raise CommandError(msg)
        args = _parse_args(arg or [])
        try:
            rendered = prompt.render(**args)
        except (MissingPromptParamError, UnknownPromptParamError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(rendered)
