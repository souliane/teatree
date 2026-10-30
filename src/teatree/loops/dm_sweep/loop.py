"""``dm_sweep`` mini-loop registration — the hourly owner-DM hygiene pass (#3658).

Registered so :func:`teatree.loops.registry.iter_loops` discovers it and the seeded
``Loop`` row has a home (the seed/registry parity invariant). The pass itself is one
scanner job whose ``scan`` runs :func:`~teatree.core.owner_dm_sweep.run_sweep`; the
loop-table fan-out drives the hourly cadence off the DB row, like every other loop.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

DM_SWEEP_LOOP_NAME = "dm_sweep"
DM_SWEEP_DEFAULT_CADENCE_SECONDS = 3600


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    """One global sweep job."""
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 (lazy import)
    from teatree.loop.scanners.dm_sweep import DmSweepScanner  # noqa: PLC0415 (lazy import)

    return [_ScannerJob(scanner=DmSweepScanner(), overlay="")]


MINI_LOOP = MiniLoop(
    name=DM_SWEEP_LOOP_NAME,
    default_cadence_seconds=DM_SWEEP_DEFAULT_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    declared_reach=frozenset(),
    determinism=LoopDeterminism.DETERMINISTIC,
)
