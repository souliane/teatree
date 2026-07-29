"""``t3 <overlay> signals`` — read-only derived-on-read factory quality signals (SIG-PR-1).

Thin wrapper over :func:`teatree.core.factory.factory_signals.compute_factory_signals`,
mirroring ``standup``/``cost``: the report is routed through the machine-output
seam — JSON on stdout under ``--json``, the human markdown on stderr — and
returned as the typed payload. Every query underneath is a select — no state
mutation, no LLM calls, no network.
"""

import os
from typing import IO, Annotated, cast

import typer

from teatree.core.factory.factory_signals import FactorySignalsReportDict, compute_factory_signals
from teatree.core.machine_output import MachineOutputCommand, emit


class Command(MachineOutputCommand):
    def handle(
        self,
        *,
        window_days: Annotated[
            int,
            typer.Option("--window-days", help="Trailing window width in days (default 28)."),
        ] = 28,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the human view."),
        ] = False,
    ) -> FactorySignalsReportDict:
        """Print the five factory signals over the trailing window vs its baseline."""
        report = compute_factory_signals(
            window_days=window_days,
            overlay=os.environ.get("T3_OVERLAY_NAME", ""),
        )
        payload = report.to_dict()
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=report.to_markdown(),
        )
        return payload
