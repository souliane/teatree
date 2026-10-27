"""Durable disposition for a reviewed ticket with no recorded shippable diff."""

from functools import partial

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from teatree.core.modelkit.gate_registry import get_resolver
from teatree.core.models.ticket import Ticket


def shipping_hold_of(ticket: Ticket) -> dict[str, str] | None:
    raw = (ticket.extra or {}).get("shipping_hold")
    return raw if isinstance(raw, dict) else None


def is_shipping_held(ticket: Ticket) -> bool:
    return shipping_hold_of(ticket) is not None


def held_author_tickets() -> QuerySet[Ticket]:
    return Ticket.objects.filter(
        state=Ticket.State.SELF_REVIEWED,
        role=Ticket.Role.AUTHOR,
        extra__has_key="shipping_hold",
    )


def hold_unshippable_review(ticket: Ticket) -> None:
    if is_shipping_held(ticket):
        return
    skipped = str((ticket.extra or {}).get("shipping_skipped") or "")
    if not skipped:
        return
    rowless = not ticket.worktrees.exists()
    ticket.merge_extra(
        set_keys={
            "shipping_hold": {
                "reason": "no worktree row" if rowless else "awaiting the workspace and forge check",
                "skipped": skipped,
                "held_at": timezone.now().isoformat(),
                "checked_at": "",
            }
        },
        pop_keys=["shipping_skipped"],
    )
    if rowless:
        transaction.on_commit(partial(get_resolver("shipping_hold_notice"), ticket.pk), robust=True)
