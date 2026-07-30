"""The ``workspace doctor`` body — cross-store drift detection and optional repair.

Its own module for the reason every sibling here has one: the CLI method stays a
thin wrapper so :mod:`teatree.core.management.commands.workspace` remains under the
module-health LOC cap. The concern is separate too — this READS every store and
reports their disagreement, where the rest of ``workspace`` drives the worktree FSM.
"""

from teatree.core.management.commands._workspace.cleanup import _fix_drift
from teatree.core.models import Ticket
from teatree.core.worktree.reconcile import reconcile_all, reconcile_ticket


def run_drift_report(*, ticket_pk: int, fix: bool) -> list[str]:
    """Report drift across Django, git worktrees, databases, docker and env caches.

    *ticket_pk* ``0`` reconciles every ticket. With *fix* the repair actions run and
    are reported inline; without it the caller is told how to apply them. Every action
    funnels through ``run_checked`` — no silent swallow.
    """
    if ticket_pk:
        drift = reconcile_ticket(Ticket.objects.get(pk=ticket_pk))
        drifts = {ticket_pk: drift} if drift.has_drift else {}
    else:
        drifts = reconcile_all()
    if not drifts:
        return ["No drift detected."]

    lines: list[str] = []
    for pk, drift in sorted(drifts.items()):
        lines.append(f"Ticket #{pk}:")
        lines.extend(f"  {finding}" for finding in drift.format().splitlines())
        if fix:
            lines.extend(f"  [fix] {msg}" for msg in _fix_drift(drift))
    if not fix:
        lines.extend(("", "Rerun with --fix to apply fixes."))
    return lines


__all__ = ["run_drift_report"]
