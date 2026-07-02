"""``manage.py loop_verify_cron <name>`` — verify-by-reread a loop's cron registration (#1192).

Backs ``t3 loop verify-cron <name>``. A CLI cannot call the harness
``CronCreate``/``CronList`` tools itself (:mod:`teatree.loops.claude_specs`), so
this command judges a ``CronList`` snapshot the agent already fetched — it never
reaches the harness. Run it right after ``CronCreate`` to confirm the
registration is actually visible rather than trusting the tool call's own
success.
"""

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from django_typer.management import TyperCommand

from teatree.core.models import Loop
from teatree.loops.claude_specs import claude_loop_spec, verify_loop_registered


class Command(TyperCommand):
    help = "Verify-by-reread: confirm a loop's CronCreate registration against a CronList snapshot (#1192)."

    def handle(
        self,
        name: Annotated[str, typer.Argument(help="DB Loop name (e.g. review, ship, dream).")],
        *,
        cron_list_json: Annotated[
            str,
            typer.Option(
                "--cron-list-json",
                help="Path to a CronList JSON snapshot (a bare JSON array of job objects), or '-' for stdin.",
            ),
        ] = "-",
    ) -> None:
        """Verify *name*'s native Claude ``/loop`` registration against a ``CronList`` snapshot."""
        loop = Loop.objects.filter(name=name).first()
        if loop is None:
            self.stderr.write(f"unknown loop {name!r} — no such DB Loop row (see `t3 loops list`).")
            raise SystemExit(1)

        raw = sys.stdin.read() if cron_list_json == "-" else Path(cron_list_json).read_text(encoding="utf-8")
        try:
            jobs = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError as exc:
            self.stderr.write(f"could not parse CronList JSON: {exc}")
            raise SystemExit(1) from exc
        if not isinstance(jobs, list):
            self.stderr.write("CronList JSON must be a bare array of job objects.")
            raise SystemExit(1)

        spec = claude_loop_spec(loop)
        outcome = verify_loop_registered(spec, jobs)
        if outcome.confirmed:
            self.stdout.write(f"confirmed: {spec.slot_id} is registered ({spec.cron}).")
            return
        self.stderr.write(f"NOT confirmed: {outcome.reason}")
        raise SystemExit(1)
