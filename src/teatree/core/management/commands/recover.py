"""``t3 recover`` — find and recover work stranded by a network-outage death (#1764).

Thin wrapper over :mod:`teatree.core.worktree.recover`. Default is a DRY-RUN typed report
(groups: data-loss risk / committed-unpushed / open-PR pending / re-queue
candidates), every ref a clickable URL. ``--requeue`` reopens the
genuinely-incomplete FAILED tasks; ``--json`` emits the structured report. The
boot sweeps (replay/reclaim/reap) always run — they are idempotent recovery.
Stranded work is surfaced for salvage (push to a PR), never auto-captured.
"""

from typing import IO, Annotated, cast

import typer
from django_typer.management import command

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.worktree.recover import RecoverReportDict, gather_recover_report, requeue_failed_tasks


class RecoverPayload(RecoverReportDict):
    """The report plus the task pks ``--requeue`` reopened (empty on the dry run)."""

    reopened_task_pks: list[int]


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

        reopened: list[int] = []
        if requeue:
            reopened = requeue_failed_tasks(report)

        payload = RecoverPayload(**report.to_dict(), reopened_task_pks=reopened)
        lines = [report.to_terse(dry_run=dry_run)]
        if requeue:
            lines.append(f"Reopened {len(reopened)} task(s): {', '.join(f'#{pk}' for pk in reopened) or '(none)'}")
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(lines),
        )
        return payload
