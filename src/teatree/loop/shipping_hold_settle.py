"""Bounded recurring settlement of reviews held for uncertain shipping evidence."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from teatree.core.managers_task_claim import redispatch_window
from teatree.core.models.shipping_hold import held_author_tickets, shipping_hold_of
from teatree.core.models.ticket import Ticket
from teatree.core.models.ticket_worktree_checks import WorktreeProbeUnverifiableError, worktree_has_commits_ahead
from teatree.core.models.types import ShippingHold
from teatree.core.models.worktree import Worktree
from teatree.loop.shipping_hold_evidence import disk_evidence, forge_evidence

logger = logging.getLogger(__name__)
RECHECK_INTERVAL = timedelta(minutes=15)
PER_PASS_CAP = 10
ROWLESS_REASON = "no worktree row; nothing on the workspaces root or the forge"


@dataclass(frozen=True)
class Settlement:
    ticket_pk: int
    action: str
    reason: str
    observed_worktrees: frozenset[tuple[int, str, str, str]] = frozenset()
    observed_branch: str = ""


class SettlementPassError(RuntimeError):
    def __init__(self, message: str, applied: list[Settlement]) -> None:
        super().__init__(message)
        self.applied = tuple(applied)


def _worktree_snapshot(rows: list[Worktree]) -> frozenset[tuple[int, str, str, str]]:
    return frozenset(
        (row.pk, row.repo_path, row.branch, str((row.extra or {}).get("worktree_path") or "")) for row in rows
    )


def _checked_at(ticket: Ticket) -> datetime | None:
    hold = shipping_hold_of(ticket) or {}
    try:
        stamp = datetime.fromisoformat(str(hold.get("checked_at") or ""))
        return timezone.make_aware(stamp) if timezone.is_naive(stamp) else stamp
    except ValueError:
        return None


def _decide(ticket: Ticket) -> Settlement:
    unknown = ""
    observed_branch = str((ticket.extra or {}).get("branch") or "")
    rows = list(ticket.worktrees.all())
    for row in rows:
        try:
            if worktree_has_commits_ahead(row):
                return Settlement(ticket.pk, "release", f"recorded checkout {row.worktree_path}")
        except WorktreeProbeUnverifiableError as exc:
            unknown = f"git ({exc})"
    disk = disk_evidence(ticket)
    if disk.found:
        return Settlement(ticket.pk, "hold", disk.found)
    forge = forge_evidence(ticket)
    if forge.found:
        return Settlement(ticket.pk, "hold", forge.found)
    unknown = unknown or disk.unverified or forge.unverified
    if unknown:
        return Settlement(ticket.pk, "hold", f"unverified: {unknown}")
    if not rows:
        return Settlement(ticket.pk, "hold", ROWLESS_REASON)
    return Settlement(ticket.pk, "ignore", "every source verified empty", _worktree_snapshot(rows), observed_branch)


def _apply_settlement(ticket_pk: int, result: Settlement, *, now: datetime) -> Settlement | None:
    with redispatch_window() as refusal:
        if refusal:
            return None
        ticket = Ticket.objects.select_for_update().get(pk=ticket_pk)  # select-for-update: caller-atomic
        hold = shipping_hold_of(ticket)
        if ticket.state != Ticket.State.SELF_REVIEWED or hold is None:
            return None
        if (checked := _checked_at(ticket)) is not None and now - checked < RECHECK_INTERVAL:
            return None
        if result.action == "ignore":
            current_rows = list(ticket.worktrees.select_for_update())  # select-for-update: caller-atomic
            if not current_rows:
                result = Settlement(ticket_pk, "hold", ROWLESS_REASON)
            elif _worktree_snapshot(current_rows) != result.observed_worktrees:
                result = Settlement(ticket_pk, "hold", "worktree rows changed; awaiting the workspace and forge check")
            elif str((ticket.extra or {}).get("branch") or "") != result.observed_branch:
                result = Settlement(ticket_pk, "hold", "recorded branch changed; awaiting the forge check")
        if result.action == "release":
            ticket.merge_extra(pop_keys=["shipping_hold"])
            ticket.schedule_shipping()
        elif result.action == "ignore":
            ticket.merge_extra(set_keys={"shipping_skipped": hold["skipped"]}, pop_keys=["shipping_hold"])
            ticket.ignore()
            ticket.save()
        else:
            updated = ShippingHold(
                reason=result.reason,
                skipped=hold["skipped"],
                held_at=hold["held_at"],
                checked_at=now.isoformat(),
            )
            ticket.merge_extra(set_keys={"shipping_hold": updated})
        return result


def settle_held_tickets(*, dry_run: bool = False, now: datetime | None = None) -> list[Settlement]:
    current = now or timezone.now()
    due = [
        ticket
        for ticket in held_author_tickets()
        if (checked := _checked_at(ticket)) is None or current - checked >= RECHECK_INTERVAL
    ]
    due.sort(key=lambda ticket: (_checked_at(ticket) or datetime.min.replace(tzinfo=current.tzinfo), ticket.pk))
    results = []
    failures = []
    for ticket in due[:PER_PASS_CAP]:
        try:
            result = _decide(ticket)
            if dry_run:
                results.append(result)
            elif applied := _apply_settlement(ticket.pk, result, now=current):
                results.append(applied)
        except Exception as exc:
            logger.exception("Shipping-hold settlement failed for ticket %s", ticket.pk)
            failures.append(f"{ticket.pk}: {type(exc).__name__}: {exc}")
            if not dry_run:
                try:
                    _apply_settlement(
                        ticket.pk,
                        Settlement(ticket.pk, "hold", f"unverified: settlement failed ({type(exc).__name__}: {exc})"),
                        now=current,
                    )
                except Exception as stamp_error:
                    logger.exception("Shipping-hold failure stamp failed for ticket %s", ticket.pk)
                    failures.append(f"{ticket.pk} stamp: {type(stamp_error).__name__}: {stamp_error}")
    if failures:
        msg = f"shipping-hold settlement failed: {'; '.join(failures)}"
        raise SettlementPassError(msg, results)
    return results
