from datetime import datetime
from typing import TYPE_CHECKING, cast

from django.db import transaction

from teatree.core.models.dream_gap_ledger import PENDING_KEY, pending_entries, undispositioned_gap_keys
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from teatree.core.models.dream_gap_types import DreamGapEntry

HOST_GAP_CAP = 3


def host_gap_capacity_refusal(host: Ticket, entries: list["DreamGapEntry"]) -> str:
    declined = [entry["gap_key"] for entry in entries if host.pk in entry.get("declined_hosts", [])]
    if declined:
        return f"host already declined gap(s): {', '.join(declined)}"
    existing = set(undispositioned_gap_keys(host))
    if len(existing | {entry["gap_key"] for entry in entries}) > HOST_GAP_CAP:
        return f"host exceeds {HOST_GAP_CAP} undispositioned gaps"
    return ""


def mark_offered(ticket: Ticket, gap_keys: set[str], *, at: datetime) -> None:
    with transaction.atomic():
        locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
        pending = pending_entries(locked)
        stamped = [
            cast("DreamGapEntry", {**entry, "offered_at": at.isoformat()})
            if entry.get("gap_key") in gap_keys
            else entry
            for entry in pending
        ]
        if stamped != pending:
            locked.merge_extra(set_keys={PENDING_KEY: stamped})
