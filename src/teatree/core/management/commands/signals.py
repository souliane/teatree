"""``t3 <overlay> signals`` — read-only derived-on-read factory quality signals (SIG-PR-1).

Thin wrapper over :func:`teatree.core.factory.factory_signals.compute_factory_signals`,
mirroring ``standup``/``cost``: the structured report is the output channel
(``django-typer`` serialises the return). Every query underneath is a select —
no state mutation, no LLM calls, no network.
"""

import json
import os
from typing import Annotated

import typer
from django_typer.management import TyperCommand

from teatree.core.factory.factory_signals import compute_factory_signals


class Command(TyperCommand):
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
    ) -> str:
        """Print the five factory signals over the trailing window vs its baseline."""
        report = compute_factory_signals(
            window_days=window_days,
            overlay=os.environ.get("T3_OVERLAY_NAME", ""),
        )
        if json_output:
            return json.dumps(report.to_dict())
        return report.to_markdown()
