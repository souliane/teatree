"""The tickets intake can never reach again — the only record of a request someone was told is tracked."""

from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db.models import Min, QuerySet

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def unfindable_tickets(tickets: QuerySet) -> list["Ticket"]:
    """Non-terminal rows no forge query can reach, oldest lane first (#4527).

    Intake discovers candidates from forge queries, so a row failing
    :meth:`~teatree.core.models.ticket.Ticket.is_admissible` can never be
    admitted, claimed, or found again — it is the only surviving record of a
    request someone was told is tracked. A row that already recorded where its
    work went is EXCLUDED: a Slack lane's bookkeeping row is non-admissible by
    design, so without that stamp the mechanism reports its own successes and
    buries the genuinely dead rows under one entry per inbound DM.

    The doctor WARN and ``ticket dead-rows`` share this one selector, so they
    can never disagree about WHICH rows are unfindable; the doctor additionally
    withholds a row younger than its grace period, which is a reporting choice
    layered on top of this set, not a second definition of it.

    ``is_admissible`` is applied in Python, not as a query: it is the single
    predicate every promise-time surface consults, and a hand-written SQL twin
    is how the check and the promise drift apart. ``Ticket`` carries no creation
    stamp, so age is its oldest ``Task``'s; a row with no task at all sorts
    FIRST — it is the most provably dead shape there is.
    """
    ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))

    rows = (
        tickets.exclude(state__in=ticket_model._SETTLED_STATES)  # noqa: SLF001 — the model's SSOT terminal set
        .annotate(oldest_task=Min("tasks__created_at"))
        .order_by("pk")
    )
    unfindable = [ticket for ticket in rows if not (ticket.is_admissible() or ticket.work_placed_elsewhere())]
    return sorted(unfindable, key=lambda row: (row.oldest_task is not None, row.oldest_task))
