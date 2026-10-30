"""Release a repeated checkout park only after the checkout fence passes again."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.worktree.checkout_fence import CheckoutFenceError, verify_task_checkout, writes_checkout
from teatree.loop.scanners.base import ScanSignal, recent_iso_stamp

_RECHECK = timedelta(hours=24)
_SCAN_LIMIT = 50


@dataclass(slots=True)
class StuckCheckoutRecheckScanner:
    name: str = "stuck_checkout_recheck"

    @staticmethod
    def scan() -> list[ScanSignal]:
        now = timezone.now()
        signals: list[ScanSignal] = []
        checked = 0
        rows = Ticket.objects.filter(extra__stuck_halt__reason__startswith="checkout parked: ").order_by("pk")
        for ticket in rows.iterator(chunk_size=100):
            if checked >= _SCAN_LIMIT:
                break
            stamp = (ticket.extra or {}).get("stuck_halt")
            if not isinstance(stamp, dict) or not str(stamp.get("reason", "")).startswith("checkout parked: "):
                continue
            task = Task.objects.filter(pk=stamp.get("newest_task"), ticket=ticket, status=Task.Status.PENDING).first()
            if task is None or Task.objects.filter(ticket=ticket, pk__gt=task.pk).exists():
                ticket.merge_extra(pop_keys=["stuck_halt"])
                continue
            if not writes_checkout(task) or not _claim_recheck(ticket, now):
                continue
            checked += 1
            path = dispatch_worktree_path(ticket)
            if not path:
                continue
            try:
                verify_task_checkout(task, path)
            except CheckoutFenceError:
                continue
            updated = Task.objects.filter(pk=task.pk, status=Task.Status.PENDING, not_before__gt=now).update(
                not_before=now, admitted_at=None
            )
            if updated or Task.objects.filter(pk=task.pk, status=Task.Status.PENDING, not_before__lte=now).exists():
                ticket.merge_extra(pop_keys=["stuck_halt"])
                signals.append(
                    ScanSignal(
                        kind="checkout_park.rearmed",
                        summary=f"Task {task.pk} checkout fence passed; repeated park released",
                        payload={"task_id": task.pk, "ticket_id": ticket.pk},
                    )
                )
        return signals


def _claim_recheck(ticket: Ticket, now: datetime) -> bool:
    with transaction.atomic():
        locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
        stamp = (locked.extra or {}).get("stuck_halt")
        if not isinstance(stamp, dict):
            return False
        if recent_iso_stamp(stamp.get("recheck_at"), now=now, window=_RECHECK):
            return False
        locked.merge_extra(merge_into_dicts={"stuck_halt": {"recheck_at": now.isoformat()}})
        return True
