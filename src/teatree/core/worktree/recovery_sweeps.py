"""Boot/tick recovery sweeps — the single SSOT shared by the loop and ``t3 recover``.

Four idempotent sweeps, ordered so a recoverable row is rescued before a harsher
sweep can fail it: ``replay_orphaned_transitions`` (#883) replays an FSM
transition a mid-transition crash dropped; ``reclaim_orphaned_claims`` (#652)
returns an expired-lease CLAIMED task to PENDING so another open session resumes
it; ``reap_stale_claims`` fails any residual stale CLAIMED row; and
``reclaim_dead_owner_leases`` (#3571) orphans a ``loop:<name>``/``t3-master`` lease
whose owner stopped holding it so the live worker stops SKIPping the loop.
The three task sweeps wait while no claim is admitted, re-reading the refusal inside
their write transaction; the lease reclaim runs under any posture.

Lives in ``teatree.core`` (not ``teatree.loop``) so ``t3 recover`` (#1764) can
compose it without ``core`` depending on ``loop`` — the dependency direction the
architecture enforces. ``loop/tick_recovery`` imports it from here.
"""

from dataclasses import dataclass

from teatree.core.managers_task_claim import redispatch_window
from teatree.core.models import LoopLease, Task


@dataclass(frozen=True, slots=True)
class BootSweepCounts:
    """How many rows each boot/tick recovery sweep acted on."""

    replayed_transitions: int = 0
    reclaimed_claims: int = 0
    reaped_claims: int = 0
    reclaimed_leases: int = 0

    @property
    def changed_rows(self) -> int:
        return self.replayed_transitions + self.reclaimed_claims + self.reaped_claims + self.reclaimed_leases


def run_boot_sweeps() -> BootSweepCounts:
    """Run the sweeps in rescue-before-fail order and return per-sweep counts."""
    with redispatch_window() as refusal:
        replayed = 0 if refusal else Task.objects.replay_orphaned_transitions()
    with redispatch_window() as refusal:
        # Reap only beside its reclaim: alone it would fail an orphan the held reclaim re-queues after the lift.
        reclaimed, reaped = (
            (0, 0) if refusal else (Task.objects.reclaim_orphaned_claims(), Task.objects.reap_stale_claims())
        )
    return BootSweepCounts(
        replayed_transitions=replayed,
        reclaimed_claims=reclaimed,
        reaped_claims=reaped,
        reclaimed_leases=len(LoopLease.objects.reclaim_dead_owner_leases()),
    )
