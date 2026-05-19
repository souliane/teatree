"""``manage.py loop_tick`` — one tick of the fat loop as a Django management command.

Scans all overlays in parallel, dispatches signals, executes mechanical
actions, and renders the statusline.  Called from ``t3 loop tick`` which
delegates here via subprocess.
"""

import datetime as dt
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
        "skipped": False,
        "skipped_reason": "",
    }


def _skipped_report_dict(started_at: dt.datetime, reason: str) -> ReportDict:
    """The full report contract for a tick that skipped (sibling holds the lease).

    #744 defect 1: a skipped tick must still emit every contract key
    (zeroed) so a coordinator pumping ``t3 loop tick --json`` can
    ``json.load(...)["signal_count"]`` / ``["errors"]`` unconditionally
    — the bare ``{"skipped": ...}`` object ``KeyError``-ed every
    structured consumer on each contended beat.
    """
    return {
        "started_at": started_at.isoformat(),
        "signal_count": 0,
        "action_count": 0,
        "statusline_path": "",
        "errors": {},
        "actions": [],
        "skipped": True,
        "skipped_reason": reason,
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
        import os  # noqa: PLC0415

        from teatree.core.backend_factory import (  # noqa: PLC0415
            code_host_from_overlay,
            iter_overlay_backends,
            messaging_from_overlay,
        )
        from teatree.core.connector_preflight import run_connector_preflight  # noqa: PLC0415
        from teatree.core.models import LoopLease  # noqa: PLC0415
        from teatree.loop.tick import TickRequest, run_tick  # noqa: PLC0415

        # Refuse to tick into silent no-ops when a hard-dependency
        # connector is unreachable. Raises SystemExit naming the down
        # connector, before the lease is taken, so the loop fails fast
        # instead of degrading.
        run_connector_preflight(overlay)

        # #786 WS2: DB lease/heartbeat replaces the flock/pidfile singleton.
        # The lease row is queryable and reapable by expiry, so loop
        # ownership survives context compaction (re-acquirable) instead of
        # silently vanishing with the flock-holding process. Acquisition is
        # a backend-agnostic atomic CAS (correct on the production SQLite
        # backend — the #786 B1 lesson), so exactly one of N concurrent
        # ticks proceeds; the rest SKIP, same contract as the old flock.
        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire("loop-tick", owner=owner):
            from teatree.loop.tick import _write_tick_meta  # noqa: PLC0415

            reason = "another tick is already running"
            now = dt.datetime.now(tz=dt.UTC)
            # #744 defect 2: a sibling tick holds the lease and IS
            # keeping the loop fresh — advance tick-meta's next_epoch so
            # sustained multi-session contention never decays into a
            # false `tick stale` on the statusline.
            _write_tick_meta(now, target=statusline_file)
            if json_output:
                # #744 defect 1: full contract shape so structured
                # consumers index unconditionally (no KeyError on skip).
                self.stdout.write(json.dumps(_skipped_report_dict(now, reason), indent=2))
            else:
                self.stdout.write(f"SKIP  {reason} — loop-tick lease held.")
            return
        try:
            if overlay:
                request = TickRequest(host=code_host_from_overlay(), messaging=messaging_from_overlay())
            else:
                request = TickRequest(backends=iter_overlay_backends())
            report = run_tick(request, statusline_path=statusline_file)
        finally:
            LoopLease.objects.release("loop-tick", owner=owner)

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
