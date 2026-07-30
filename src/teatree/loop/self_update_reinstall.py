"""Deferred drain for the self-update scanner's reinstall queue.

The self-update scanner fast-forwards an editable clone but never
re-anchors the running interpreter on the new code — a ``git pull`` alone
leaves the live process importing the old modules. When
``auto_update_reinstall`` is enabled the scanner records a
:class:`teatree.core.models.pending_reinstall.PendingReinstall` row; this
module applies it.

``drain_pending_reinstall`` is called as an early step of the ``loops_tick``
per-loop command (lease-guarded, at most once per drain-throttle window) — a
fresh per-tick subprocess, before any scanner code imports — so the
``uv tool install --editable <src> --reinstall`` + ``t3 setup`` + self-DB
migrate runs in a process that has not yet imported the about-to-change
code (no mixed-code window). It is a no-op when nothing is pending, and
DEFERS (leaves the row pending, returns ``deferred``) whenever a loop
unit is in flight — a live CLAIMED task lease — so the code is never
mutated out from under an active sub-agent.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.models.pending_reinstall import PendingReinstall

logger = logging.getLogger(__name__)


class DrainOutcome(Enum):
    NOOP = "noop"
    DEFERRED = "deferred"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DrainResult:
    outcome: DrainOutcome
    repo_label: str = ""
    detail: str = ""


def drain_pending_reinstall() -> DrainResult:
    """Apply one pending deferred reinstall, or defer / no-op.

    Order is load-bearing: the pending-row check comes first so a clean
    tick (the overwhelming majority) does zero extra work; the in-flight
    check is only consulted when there is actually something to drain.
    """
    from teatree.core.models.pending_reinstall import PendingReinstall  # noqa: PLC0415 — deferred: ORM/app-registry

    row = PendingReinstall.objects.pending().first()
    if row is None:
        return DrainResult(outcome=DrainOutcome.NOOP)
    if _loop_unit_in_flight():
        logger.info("self_update_reinstall deferred for %s — a loop unit is in flight", row.repo_label)
        return DrainResult(outcome=DrainOutcome.DEFERRED, repo_label=row.repo_label)
    return _apply(row)


def _loop_unit_in_flight() -> bool:
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry

    return Task.objects.active_claim_exists()


def _apply(row: "PendingReinstall") -> DrainResult:
    from teatree.self_update import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
        ensure_self_db_migrated,
        reinstall_running_editable,
    )

    label = row.repo_label
    result = reinstall_running_editable()
    if not result.ok:
        row.mark_failed(result.error)
        logger.warning("self_update_reinstall failed for %s: %s", label, result.error)
        return DrainResult(outcome=DrainOutcome.FAILED, repo_label=label, detail=result.error)
    if ensure_self_db_migrated():
        detail = "self-DB left unmigrated"
        row.mark_failed(detail)
        logger.warning("self_update_reinstall left self-DB unmigrated for %s", label)
        return DrainResult(outcome=DrainOutcome.FAILED, repo_label=label, detail=detail)
    row.mark_done()
    logger.info("self_update_reinstall applied for %s", label)
    return DrainResult(outcome=DrainOutcome.DONE, repo_label=label)


__all__ = ["DrainOutcome", "DrainResult", "drain_pending_reinstall"]
