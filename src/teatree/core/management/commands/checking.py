"""``t3 <overlay> checking show`` — terse, read-only "what did I miss" report (#1529).

Thin wrapper over :func:`teatree.core.checking.gather_checking_report`. The
command reads the prior checkpoint, gathers the window ``[window_start, now)``,
and advances the marker to ``now`` *after* gathering — so an immediate second
run reports an empty window rather than the first run collapsing its own
window. The marker advances ONLY on the default path: ``--since`` (the user
named an explicit window) and ``--no-advance`` (an inspection-only run) both
leave the checkpoint untouched.

Read-only: every query underneath is a select; the command never transitions a
ticket nor writes any row except the checkpoint marker. The return value is the
output channel (``django-typer`` serialises it) — JSON when ``--json``, else
the terse human view.
"""

import json
import os
from typing import Annotated

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command, initialize

from teatree.core.checking import gather_checking_report
from teatree.core.checkpoint import advance_checkpoint_monotonic, resolve_window_start


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> checking`` group root."""

    @command()
    def show(
        self,
        *,
        since: Annotated[
            str,
            typer.Option(help="ISO timestamp override for the window start (does NOT advance the marker)."),
        ] = "",
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the terse view."),
        ] = False,
        no_advance: Annotated[
            bool,
            typer.Option("--no-advance", help="Read the window without advancing the last-checked marker."),
        ] = False,
    ) -> str:
        """Print a terse, grouped, clickable report of changes since the last check."""
        overlay_name = os.environ.get("T3_OVERLAY_NAME", "")
        now = timezone.now()
        window_start = resolve_window_start(since=since, now=now)
        report = gather_checking_report(
            since=window_start,
            now=now,
            overlay_name=overlay_name,
            code_host=self._resolve_code_host(),
            overlay_repos=self._resolve_overlay_repos(),
        )
        # Advance only on the default path: an explicit --since or --no-advance
        # is an inspection that must not move the user's last-checked marker.
        # The advance is monotonic — it never writes a marker earlier than the
        # stored one, so a clock regression or a future/skewed marker cannot
        # collapse a real window or mark unreported events as seen.
        if not since and not no_advance:
            advance_checkpoint_monotonic(now)
        if json_output:
            return json.dumps(report.to_dict())
        return report.to_terse(overlay_name=overlay_name)

    @staticmethod
    def _resolve_code_host() -> str:
        """Resolve the overlay's ``code_host`` string for the URL builder (no forge call).

        Reads ``overlay.config.code_host`` directly — a pure config read, never
        a network call. A missing or unloadable overlay degrades to an empty
        host (the builder then defaults to the GitHub URL shape).
        """
        try:
            from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

            return get_overlay().config.code_host or ""
        except Exception:  # noqa: BLE001 — config read must never wedge a read-only report
            return ""

    @staticmethod
    def _resolve_overlay_repos() -> list[str]:
        """Resolve the overlay's repo identifiers used to scope NULL-ticket merges (#1559).

        Unions ``get_followup_repos()`` (``owner/repo``) with ``get_repos()``
        (often a bare ``repo`` name) so a ceremony CLEAR whose resolved repo
        matches either shape is scoped to this overlay. A missing or unloadable
        overlay (or a hook that raises) degrades to an empty list — the merged
        group then keeps the ticket-bearing back-compat scope only.
        """
        try:
            from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

            overlay = get_overlay()
            repos = list(overlay.metadata.get_followup_repos()) + list(overlay.get_repos())
            return [repo for repo in repos if isinstance(repo, str) and repo]
        except Exception:  # noqa: BLE001 — config read must never wedge a read-only report
            return []
