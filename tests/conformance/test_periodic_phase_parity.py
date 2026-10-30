"""Every maintenance phase with periodic task work has one declaring mini-loop."""

from teatree.core.modelkit.phases import ARCHITECTURAL_REVIEW_PHASE, MAINTENANCE_PHASES
from teatree.loops.registry import iter_loops


def test_periodic_phase_declarations_cover_maintenance_tasks_once() -> None:
    phases = [loop.periodic_phase for loop in iter_loops() if loop.periodic_phase]

    assert len(phases) == len(set(phases))
    assert set(phases) == MAINTENANCE_PHASES | {ARCHITECTURAL_REVIEW_PHASE}
