"""``t3 recover`` — find and recover work stranded by a network-outage death (#1764).

Thin wrapper over :mod:`teatree.core.worktree.recover`. Default is a DRY-RUN typed report
(groups: data-loss risk / committed-unpushed / open-PR pending / re-queue
candidates), every ref a clickable URL. ``--requeue`` reopens the
genuinely-incomplete FAILED tasks; ``--json`` emits the structured report. The
boot sweeps (replay/reclaim/reap) always run — idempotent recovery that, like
``--requeue``, holds its task writes while no claim is admitted.
Stranded work is surfaced for salvage (push to a PR), never auto-captured.
"""

from typing import IO, Annotated, cast

import typer
from django_typer.management import command

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.worktree.recover import RecoverReportDict, RequeueOutcome, gather_recover_report, requeue_failed_tasks


class RecoverPayload(RecoverReportDict):
    """The report plus the task pks ``--requeue`` reopened, or held back while no claim is admitted."""

    reopened_task_pks: list[int]
    held_task_pks: list[int]
    held_reason: str


class Command(MachineOutputCommand):
    @command()
    def recover(
        self,
        *,
        requeue: Annotated[
            bool,
            typer.Option("--requeue", help="Reopen genuinely-incomplete FAILED (incl. outage-death) tasks."),
        ] = False,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON."),
        ] = False,
    ) -> RecoverPayload:
        """Report (and optionally recover) work stranded by an outage."""
        dry_run = not requeue
        report = gather_recover_report()

        outcome = requeue_failed_tasks(report) if requeue else RequeueOutcome()

        payload = RecoverPayload(
            **report.to_dict(),
            reopened_task_pks=outcome.reopened,
            held_task_pks=outcome.held,
            held_reason=outcome.held_reason,
        )
        lines = [report.to_terse(dry_run=dry_run)]
        if requeue:
            reopened = ", ".join(f"#{pk}" for pk in outcome.reopened) or "(none)"
            lines.append(f"Reopened {len(outcome.reopened)} task(s): {reopened}")
        if outcome.held:
            held = ", ".join(f"#{pk}" for pk in outcome.held)
            lines.append(f"Held back {len(outcome.held)} task(s): {held} — {outcome.held_reason}")
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(lines),
        )
        return payload
