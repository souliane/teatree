"""``t3 recover`` — find and recover work stranded by a network-outage death (#1764).

Thin wrapper over :mod:`teatree.core.recover`. Default is a DRY-RUN typed report
(groups: data-loss risk / committed-unpushed / open-PR pending / stranded
snapshots / re-queue candidates), every ref a clickable URL. ``--requeue``
reopens the genuinely-incomplete FAILED tasks; ``--snapshot`` force-captures
dirty/unpushed worktrees; ``--json`` emits the structured report. The boot
sweeps (replay/reclaim/reap) always run — they are idempotent recovery.
"""

import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.recover import force_capture_snapshots, gather_recover_report, requeue_failed_tasks


class Command(TyperCommand):
    @command()
    def recover(
        self,
        *,
        requeue: Annotated[
            bool,
            typer.Option("--requeue", help="Reopen genuinely-incomplete FAILED (incl. outage-death) tasks."),
        ] = False,
        snapshot: Annotated[
            bool,
            typer.Option("--snapshot", help="Force-capture a bundle+diff of every dirty/unpushed worktree."),
        ] = False,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON."),
        ] = False,
    ) -> str:
        """Report (and optionally recover) work stranded by an outage."""
        dry_run = not (requeue or snapshot)
        report = gather_recover_report()

        reopened: list[int] = []
        captured: list[str] = []
        if requeue:
            reopened = requeue_failed_tasks(report)
        if snapshot:
            captured = [str(p) for p in force_capture_snapshots()]

        if json_output:
            payload = {**report.to_dict(), "reopened_task_pks": reopened, "captured_snapshots": captured}
            return json.dumps(payload)

        lines = [report.to_terse(dry_run=dry_run)]
        if requeue:
            lines.append(f"Reopened {len(reopened)} task(s): {', '.join(f'#{pk}' for pk in reopened) or '(none)'}")
        if snapshot:
            lines.append(f"Captured {len(captured)} snapshot(s).")
            lines += [f"  {path}" for path in captured]
        return "\n".join(lines)
