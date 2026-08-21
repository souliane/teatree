"""Captured unshipped work must be VISIBLE, not merely recorded (#3891).

The capture half already exists: before any pass may reap a checkout,
:mod:`teatree.core.cleanup.unshipped_work` snapshots what that checkout holds
that exists nowhere else and writes an
:class:`~teatree.core.models.unshipped_work_record.UnshippedWorkRecord`. Nothing
read those rows, so the outcome was the same as before the capture — work sat in
scratch checkouts that no surface named, and it took a person noticing.

That is the shape the guard itself creates: a reaper correctly refuses to remove
a checkout holding work, and a permanently-kept checkout looks exactly like a
legitimately busy one. Age is what separates them, so age is what this reports —
and specifically ``first_captured_at``, never ``captured_at``. The capture pass
re-runs on every non-dry-run sweep over every kept checkout, so the last-capture
stamp is reset continuously and an age built on it reads zero forever on exactly
the host that sweeps most: a surface that always says "just now" reports nothing
at all.
"""

from datetime import timedelta
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.core.models import UnshippedWorkRecord

_LISTED = 10


def check_unshipped_work() -> bool:
    """WARN once per checkout whose captured work is still unshipped, oldest first.

    Advisory, never a gate. The rows describe work an operator has to decide
    about, not an invariant teatree broke, and a doctor FAIL that no command can
    clear is the permanently-red check that trains its reader to skip the whole
    report.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: needs Django configured

    from teatree.core.models import UnshippedWorkRecord  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        records = list(UnshippedWorkRecord.objects.order_by("first_captured_at"))
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(
            f"WARN  Unshipped-work capture UNVERIFIED: the record ledger could not be read "
            f"({exc.__class__.__name__}: {exc})."
        )
        return True
    if not records:
        return True
    now = timezone.now()
    typer.echo(
        f"WARN  {len(records)} checkout(s) hold captured unshipped work, oldest "
        f"{_age(now - records[0].first_captured_at)} old — recorded before any reaper could act on them, and still "
        "unshipped. Nothing reaps them, so they stay until someone decides. Apply one back with "
        "t3 <overlay> workspace restore <checkout> --into <checkout>, or capture the branch to a PR "
        "with t3 <overlay> workspace salvage."
    )
    for record in records[:_LISTED]:
        typer.echo(f"      {_describe(record, age=_age(now - record.first_captured_at))}")
    if len(records) > _LISTED:
        typer.echo(f"      … and {len(records) - _LISTED} more (t3 <overlay> workspace emit lists every one).")
    return True


def _describe(record: "UnshippedWorkRecord", *, age: str) -> str:
    """One record's line: what it holds, where the bundle is, and how long it has waited."""
    held = [f"{len(record.dirty_paths)} dirty path(s)", f"{len(record.unpushed_commits)} unpushed commit(s)"]
    if record.unreadable:
        # An unreadable probe counts as work: a checkout whose state could not be
        # read has not been proven empty. Cause-neutral, because half of these are
        # a venue miss rather than anything git is entitled to be blamed for.
        held.append(f"unreadable here ({record.unreadable})")
    where = f", bundle {record.artifact_prefix}" if record.artifact_prefix else ""
    branch = record.branch or "(no branch)"
    return f"{age} old — {record.checkout_path} on '{branch}': {', '.join(held)}{where}"


def _age(delta: timedelta) -> str:
    """A coarse human age; the reader needs the order of magnitude, not the seconds."""
    days = delta.days
    if days >= 1:
        return f"{days}d"
    hours = delta.seconds // 3600
    return f"{hours}h" if hours else f"{max(delta.seconds // 60, 0)}m"


__all__ = ["check_unshipped_work"]
