"""``manage.py loop_claude_spec <name>`` — one loop's native Claude ``/loop`` spec (#2650).

Backs ``t3 loop claude-spec <name>``. ORM access lives here (a management command,
not a plain typer command) per the project's "anything touching the ORM is a
management command" rule.

The DB ``Loop`` table is the single source of truth; this prints the EXACT spec —
the stable ``slot_id`` (a per-loop LABEL), the ``cron`` derived from the row's
cadence, and the recurring ``prompt`` that runs only that loop — so the
``/t3:loops`` enable/disable skill can mirror the row into Claude Code:
``CronCreate`` it on enable, ``CronList``→``CronDelete`` it on disable. On disable
the skill matches the registered job by this ``prompt`` (the backtick-terminated
``--loop <name>`` token disambiguates one loop from another) and deletes it by the
harness job id. The spec is computed from the row regardless of ``enabled`` (a
disable flips the row first, then reads the spec to find the cron).
"""

import json
from dataclasses import asdict
from typing import Annotated

import typer
from django_typer.management import TyperCommand

from teatree.core.models import Loop
from teatree.loops.claude_specs import claude_loop_spec


class Command(TyperCommand):
    help = "Print one DB Loop's native Claude /loop spec: slot_id, cron, prompt (#2650)."

    def handle(
        self,
        name: Annotated[str, typer.Argument(help="DB Loop name (e.g. review, ship, dream).")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the spec as JSON.")] = False,
    ) -> None:
        """Emit the native Claude ``/loop`` spec for the loop named *name*."""
        loop = Loop.objects.filter(name=name).first()
        if loop is None:
            # Non-zero exits run under Django's ``call_command``; ``SystemExit`` is
            # the right primitive (``typer.Exit`` is swallowed on that path).
            self.stderr.write(f"unknown loop {name!r} — no such DB Loop row (see `t3 loops list`).")
            raise SystemExit(1)

        spec = claude_loop_spec(loop)
        if json_output:
            self.stdout.write(json.dumps(asdict(spec), indent=2))
            return
        self.stdout.write(f"slot_id: {spec.slot_id}")
        self.stdout.write(f"cron:    {spec.cron}")
        self.stdout.write(f"prompt:  {spec.prompt}")
