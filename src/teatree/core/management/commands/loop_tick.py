"""``manage.py loop_tick`` — user-manual full-scan tick (autonomous-lane redesign §7).

NOT the loop driver. The automated loop is per-loop: each enabled DB ``Loop`` row
runs as its own native Claude ``/loop`` firing ``t3 loops tick --loop <name>``
(``loops_tick``). This is the by-hand diagnostic a person runs to scan every
registered overlay's scanners ONCE and render the statusline — it claims no owner
lease and is NOT gated by the DB ``Loop`` table, so it runs the full default
scanner set regardless of which loops are enabled. The system never uses it to
drive itself, and there is no master tick.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from django_typer.management import TyperCommand

from teatree.core.backend_factory import code_host_from_overlay, iter_overlay_backends, messaging_from_overlay
from teatree.core.management.commands.loops_tick import _report_to_dict

if TYPE_CHECKING:
    from teatree.loop.tick import TickRequest


class Command(TyperCommand):
    help = "Run one user-manual full-scan tick: scan every overlay once, dispatch, render the statusline."

    def _build_request(self, overlay: str) -> "TickRequest":
        from teatree.loop.tick import TickRequest  # noqa: PLC0415

        if overlay:
            return TickRequest(host=code_host_from_overlay(), messaging=messaging_from_overlay())
        return TickRequest(backends=iter_overlay_backends())

    def handle(
        self,
        *,
        statusline_file: Annotated[
            Path | None, typer.Option("--statusline-file", help="Override the statusline output path (test hook).")
        ] = None,
        overlay: Annotated[
            str, typer.Option("--overlay", help="Restrict scanning to the named overlay (default: all).")
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit the tick report as JSON.")] = False,
    ) -> None:
        from teatree.loop.statusline import set_mini_loop_schedules_reader  # noqa: PLC0415
        from teatree.loop.tick import run_tick  # noqa: PLC0415
        from teatree.loops.schedule import mini_loop_schedules  # noqa: PLC0415

        # Install the DB-backed mini-loop reader so the by-hand render still
        # carries the full per-loop countdown line, then reset the process-global
        # seam so it never leaks past the manual tick.
        set_mini_loop_schedules_reader(mini_loop_schedules)
        try:
            report = run_tick(self._build_request(overlay), statusline_path=statusline_file)
        finally:
            set_mini_loop_schedules_reader(None)

        if json_output:
            self.stdout.write(json.dumps(_report_to_dict(report), indent=2))
            return
        for name, message in report.errors.items():
            self.stdout.write(f"WARN  {name}: {message}")
