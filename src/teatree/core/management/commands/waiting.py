"""``t3 teatree waiting`` — the durable waiting-on-you lane (PR-21).

``list`` shows owner-directed decisions; ``list --all`` includes the factory's
merge, review and manual waits. ``add`` records a manual :class:`~teatree.core.models.waiting_item.WaitingItem`;
``resolve`` closes one by id. The auto-populated kinds have no ``resolve`` here —
they clear by resolving their own source (answer the question, issue the CLEAR,
approve the MR).
"""

import io
from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import command, initialize

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.models.waiting_item import WaitingItem
from teatree.core.ref_render import render_ref
from teatree.core.table_output import print_table
from teatree.core.waiting import WaitingEntry, format_age, gather_owner_waiting, gather_waiting


class _WaitingRow(TypedDict):
    id: int | None
    row_id: int | None
    owner_only: bool
    kind: str
    ref: str
    url: str
    age_seconds: int


class WaitingPayload(TypedDict):
    count: int
    entries: list[_WaitingRow]


def _payload(entries: list[WaitingEntry]) -> WaitingPayload:
    return {
        "count": len(entries),
        "entries": [
            {
                "id": entry.entry_id,
                "row_id": entry.row_id,
                "owner_only": entry.owner_only,
                "kind": entry.kind,
                "ref": entry.ref,
                "url": entry.url,
                "age_seconds": int(entry.age.total_seconds()),
            }
            for entry in entries
        ],
    }


def _entry_label(entry: WaitingEntry) -> str:
    if entry.entry_id is not None:
        return str(entry.entry_id)
    return f"Q{entry.row_id}" if entry.row_id is not None else "-"


def _render_list(entries: list[WaitingEntry]) -> str:
    """Render the waiting entries as a table, or a terse empty line."""
    if not entries:
        return "nothing waiting on you"
    buffer = io.StringIO()
    rows = [
        [
            _entry_label(entry),
            entry.kind,
            format_age(entry.age),
            render_ref(entry.ref, url=entry.url),
        ]
        for entry in entries
    ]
    print_table(["Id", "Kind", "Age", "Waiting on you"], rows, title="Waiting on you", stream=buffer)
    return buffer.getvalue().rstrip("\n")


class Command(MachineOutputCommand):
    @initialize()
    def init(self) -> None:
        """``t3 teatree waiting`` group root."""

    @command()
    def list(
        self,
        *,
        all_items: Annotated[
            bool,
            typer.Option("--all", help="Include internal merge, review, and manual waits."),
        ] = False,
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Scope merge/review entries to this overlay (default: all)."),
        ] = "",
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the entries as JSON instead of the table view."),
        ] = False,
    ) -> WaitingPayload:
        """List owner decisions, or the full factory inventory with --all."""
        entries = gather_waiting(overlay) if all_items else gather_owner_waiting()
        payload = _payload(entries)
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_render_list(entries),
        )
        return payload

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
