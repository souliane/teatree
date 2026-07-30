"""Boot/tick recovery sweeps — the single SSOT shared by the loop and ``t3 recover``.

Four idempotent sweeps, ordered so a recoverable row is rescued before a harsher
sweep can fail it: ``replay_orphaned_transitions`` (#883) replays an FSM
transition a mid-transition crash dropped; ``reclaim_orphaned_claims`` (#652)
returns an expired-lease CLAIMED task to PENDING so another open session resumes
it; ``reap_stale_claims`` fails any residual stale CLAIMED row; and
``reclaim_dead_owner_leases`` (#3571) orphans a ``loop:<name>``/``t3-master`` lease
whose owning session is provably dead so the live worker stops SKIPping the loop.

Lives in ``teatree.core`` (not ``teatree.loop``) so ``t3 recover`` (#1764) can
compose it without ``core`` depending on ``loop`` — the dependency direction the
architecture enforces. ``loop/tick_recovery`` imports it from here.
"""

from dataclasses import dataclass

from teatree.core.models import LoopLease, Task


@dataclass(frozen=True, slots=True)
class BootSweepCounts:
    """How many rows each boot/tick recovery sweep acted on."""

    replayed_transitions: int = 0
    reclaimed_claims: int = 0
    reaped_claims: int = 0
    reclaimed_leases: int = 0


def run_boot_sweeps() -> BootSweepCounts:
    """Run the sweeps in rescue-before-fail order and return per-sweep counts."""
    return BootSweepCounts(
        replayed_transitions=Task.objects.replay_orphaned_transitions(),
        reclaimed_claims=Task.objects.reclaim_orphaned_claims(),
        reaped_claims=Task.objects.reap_stale_claims(),
        reclaimed_leases=len(LoopLease.objects.reclaim_dead_owner_leases()),
    )
