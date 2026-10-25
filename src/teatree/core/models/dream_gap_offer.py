from datetime import datetime
from typing import TYPE_CHECKING, cast

from django.db import transaction

from teatree.core.models.dream_gap_ledger import PENDING_KEY, pending_entries
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from teatree.core.models.dream_gap_types import DreamGapEntry


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
