"""``manage.py loop_slack_answer`` — one reactive Slack-answer cycle (#1014).

Structural clone of ``loop_self_improve``: acquires a dedicated
``LoopLease`` (``loop-slack-answer``) so a long answer cycle never blocks
a fast regular tick or a self-improve cycle, defers when it does not hold
the t3-master gate (:mod:`teatree.core.t3_master_gate`), runs
:func:`run_slack_answer_cycle`, and prints a one-line summary (or the JSON
report when ``--json`` is passed).

This is a reactive ``/loop`` slot complementing the slower per-loop ticks —
a quick ack / status question gets a reply in seconds at near-zero token cost.
The inbound-event wake is the primary drain (~1s); this timer slot is the
fallback safety net on a 5m default cadence.
"""

import datetime as dt
import os
from dataclasses import asdict
from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand

from teatree.core.machine_output import emit
from teatree.core.t3_master_gate import t3_master_verdict


class Command(TyperCommand):
    help = "Run one reactive Slack-answer cycle (the third /loop slot)."

    def handle(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the cycle report as JSON."),
        ] = False,
    ) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
        from teatree.loop.slack_answer.cycle import run_slack_answer_cycle  # noqa: PLC0415 — lazy command import

        out = cast("IO[str]", self.stdout)
        err = cast("IO[str]", self.stderr)
        verdict = t3_master_verdict()
        if not verdict.may_run:
            now = dt.datetime.now(tz=dt.UTC)
            emit(
                {
                    "skipped": True,
                    "skipped_reason": verdict.outcome.value,
                    "owner_session": verdict.owner_session,
                    "started_at": now.isoformat(),
                },
                json_output=json_output,
                out=out,
                err=err,
                human=verdict.skip_message("Slack-answer"),
            )
            return

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire("loop-slack-answer", owner=owner):
            now = dt.datetime.now(tz=dt.UTC)
            emit(
                {
                    "skipped": True,
                    "skipped_reason": "another Slack-answer cycle is already running",
                    "started_at": now.isoformat(),
                },
                json_output=json_output,
                out=out,
                err=err,
                human="SKIP  loop-slack-answer lease held — another cycle is running.",
            )
            return
        try:
            report = run_slack_answer_cycle()
        finally:
            LoopLease.objects.release("loop-slack-answer", owner=owner)

        emit(
            asdict(report),
            json_output=json_output,
            out=out,
            err=err,
            human=(
                f"OK    processed={report.processed} acked={report.acked} "
                f"simple={report.answered_simple} dispatched={report.dispatched} "
                f"covered={report.covered} errors={report.errors}"
            ),
        )
