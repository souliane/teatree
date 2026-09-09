"""``t3 recover`` — find and recover work stranded by a network-outage death (#1764).

Thin wrapper over :mod:`teatree.core.worktree.recover`. Default is a DRY-RUN typed report
(groups: data-loss risk / committed-unpushed / open-PR pending / re-queue
candidates), every ref a clickable URL. ``--requeue`` reopens the
genuinely-incomplete FAILED tasks; ``--json`` emits the structured report. The
boot sweeps (replay/reclaim/reap) always run — they are idempotent recovery.
Stranded work is surfaced for salvage (push to a PR), never auto-captured.

The requeue is BOUNDED (#4710): ``--since`` scopes it to the incident being recovered
from, and the count is stated before anything is reopened, refusing past ``--max`` until
the operator re-runs naming that number.
"""

import datetime as dt
from typing import IO, Annotated, cast

import typer
from django_typer.management import command

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.modelkit.durations import format_window, parse_duration
from teatree.core.worktree.recover import (
    DEFAULT_MAX_REOPEN,
    DEFAULT_REQUEUE_WINDOW,
    RecoverReportDict,
    RequeueThresholdError,
    gather_recover_report,
    requeue_failed_tasks,
)

#: Rendered from the core default so the flag and the window can never drift apart.
_DEFAULT_SINCE = format_window(DEFAULT_REQUEUE_WINDOW)


class RecoverPayload(RecoverReportDict):
    """The report plus the task pks ``--requeue`` reopened (empty on the dry run)."""

    reopened_task_pks: list[int]
    requeue_refused: bool


class Command(MachineOutputCommand):
    """Report (and optionally recover) work stranded by an outage."""

    @command()
    def recover(
        self,
        *,
        requeue: Annotated[
            bool,
            typer.Option("--requeue", help="Reopen genuinely-incomplete FAILED (incl. outage-death) tasks."),
        ] = False,
        since: Annotated[
            str,
            typer.Option("--since", help="Only reopen tasks that failed within this window (e.g. 24h, 3d)."),
        ] = _DEFAULT_SINCE,
        max_reopen: Annotated[
            int,
            typer.Option(
                "--max", help="Refuse to reopen more than this many tasks; re-run naming the count to confirm."
            ),
        ] = DEFAULT_MAX_REOPEN,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON."),
        ] = False,
    ) -> RecoverPayload:
        """Report (and optionally recover) work stranded by an outage."""
        dry_run = not requeue
        window = self._resolve_window(since)
        report = gather_recover_report(since=window)

        reopened: list[int] = []
        refused = False
        lines = [report.to_terse(dry_run=dry_run)]
        if requeue:
            lines.append(report.requeue_preview())
            try:
                reopened = requeue_failed_tasks(report, max_reopen=max_reopen)
            except RequeueThresholdError as exc:
                refused = True
                lines.append(f"REFUSED: {exc}")
            else:
                lines.append(
                    f"Reopened {len(reopened)} task(s): {', '.join(f'#{pk}' for pk in reopened) or '(none)'}",
                )

        payload = RecoverPayload(**report.to_dict(), reopened_task_pks=reopened, requeue_refused=refused)
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(lines),
        )
        if refused:
            raise SystemExit(1)
        return payload

    def _resolve_window(self, since: str) -> dt.timedelta:
        try:
            return parse_duration(since, flag="--since")
        except ValueError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(1) from exc
