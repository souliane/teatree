"""``t3 recover`` — find and recover work stranded by a network-outage death (#1764).

Thin wrapper over :mod:`teatree.core.worktree.recover`. Default is a DRY-RUN typed report
(groups: data-loss risk / committed-unpushed / open-PR pending / re-queue
candidates), every ref a clickable URL. ``--requeue`` reopens the
genuinely-incomplete FAILED tasks; ``--json`` emits the structured report. The
boot sweeps (replay/reclaim/reap) always run — they are idempotent recovery.
Stranded work is surfaced for salvage (push to a PR), never auto-captured.
"""

import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.worktree.recover import gather_recover_report, requeue_failed_tasks


class Command(TyperCommand):
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
    ) -> str:
        """Report (and optionally recover) work stranded by an outage."""
        dry_run = not requeue
        report = gather_recover_report()

        reopened: list[int] = []
        if requeue:
            reopened = requeue_failed_tasks(report)

        if json_output:
            payload = {**report.to_dict(), "reopened_task_pks": reopened}
            return json.dumps(payload)

        lines = [report.to_terse(dry_run=dry_run)]
        if requeue:
            lines.append(f"Reopened {len(reopened)} task(s): {', '.join(f'#{pk}' for pk in reopened) or '(none)'}")
        return "\n".join(lines)
