"""``t3 teatree waiting`` — the durable waiting-on-you lane (PR-21).

``list`` prints every entry currently waiting on the user — questions,
PRs awaiting a merge authorization, pending review requests, and the
operator's own manual items — computed live by
:func:`teatree.core.waiting.gather_waiting`, so a resolved thing simply stops
appearing. ``add`` records a manual :class:`~teatree.core.models.waiting_item.WaitingItem`;
``resolve`` closes one by id. The auto-populated kinds have no ``resolve`` here —
they clear by resolving their own source (answer the question, issue the CLEAR,
approve the MR).
"""

import io
import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models.waiting_item import WaitingItem
from teatree.core.ref_render import render_ref
from teatree.core.table_output import print_table
from teatree.core.waiting import WaitingEntry, format_age, gather_waiting


def _render_list(entries: list[WaitingEntry]) -> str:
    """Render the waiting entries as a table, or a terse empty line."""
    if not entries:
        return "nothing waiting on you"
    buffer = io.StringIO()
    rows = [
        [
            str(entry.entry_id) if entry.entry_id is not None else "-",
            entry.kind,
            format_age(entry.age),
            render_ref(entry.ref, url=entry.url),
        ]
        for entry in entries
    ]
    print_table(["Id", "Kind", "Age", "Waiting on you"], rows, title="Waiting on you", stream=buffer)
    return buffer.getvalue().rstrip("\n")


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 teatree waiting`` group root."""

    @command()
    def list(
        self,
        *,
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Scope merge/review entries to this overlay (default: all)."),
        ] = "",
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the entries as JSON instead of the table view."),
        ] = False,
    ) -> str:
        """List everything currently waiting on the user."""
        entries = gather_waiting(overlay)
        if json_output:
            return json.dumps(
                {
                    "count": len(entries),
                    "entries": [
                        {
                            "id": entry.entry_id,
                            "kind": entry.kind,
                            "ref": entry.ref,
                            "url": entry.url,
                            "age_seconds": int(entry.age.total_seconds()),
                        }
                        for entry in entries
                    ],
                },
            )
        return _render_list(entries)

    @command()
    def add(
        self,
        text: Annotated[str, typer.Argument(help="The manual waiting-item text to record.")],
    ) -> str:
        """Record a manual waiting item the live sources cannot see."""
        item = WaitingItem.objects.add(text)
        return f"recorded waiting item {item.pk}: {item.text}"

    @command()
    def resolve(
        self,
        item_id: Annotated[int, typer.Argument(help="The manual WaitingItem id to resolve.")],
    ) -> str:
        """Resolve a manual waiting item by id."""
        if WaitingItem.objects.resolve(item_id):
            return f"resolved waiting item {item_id}"
        return f"no open waiting item {item_id}"
