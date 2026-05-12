"""``manage.py loop_tick`` — one tick of the fat loop as a Django management command.

Scans all overlays in parallel, dispatches signals, executes mechanical
actions, and renders the statusline.  Called from ``t3 loop tick`` which
delegates here via subprocess.
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from django_typer.management import TyperCommand

from teatree.loop.tick import TickReport

type ReportDict = dict[str, Any]


def _report_to_dict(report: TickReport) -> ReportDict:
    return {
        "started_at": report.started_at.isoformat(),
        "signal_count": report.signal_count,
        "action_count": report.action_count,
        "statusline_path": str(report.statusline_path) if report.statusline_path else "",
        "errors": report.errors,
        "actions": [asdict(action) for action in report.actions],
    }


class Command(TyperCommand):
    help = "Run one tick: scan all overlays, dispatch, render statusline."

    def handle(
        self,
        *,
        statusline_file: Annotated[
            Path | None,
            typer.Option("--statusline-file", help="Override the statusline output path (test hook)."),
        ] = None,
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Restrict scanning to the named overlay (default: all)."),
        ] = "",
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the tick report as JSON."),
        ] = False,
    ) -> None:
        from teatree.core.backend_factory import (  # noqa: PLC0415
            code_host_from_overlay,
            iter_overlay_backends,
            messaging_from_overlay,
        )
        from teatree.loop.tick import TickRequest, run_tick  # noqa: PLC0415

        if overlay:
            request = TickRequest(host=code_host_from_overlay(), messaging=messaging_from_overlay())
        else:
            request = TickRequest(backends=iter_overlay_backends())
        report = run_tick(request, statusline_path=statusline_file)
        result = _report_to_dict(report)
        if json_output:
            self.stdout.write(json.dumps(result, indent=2))
        else:
            self.stdout.write(f"OK    {report.signal_count} signal(s), {report.action_count} action(s).")
            if report.errors:
                for name, message in report.errors.items():
                    self.stdout.write(f"WARN  {name}: {message}")
            if report.statusline_path:
                self.stdout.write(f"      statusline → {report.statusline_path}")
