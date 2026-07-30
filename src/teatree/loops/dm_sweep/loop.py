"""``dm_sweep`` mini-loop registration — the hourly owner-DM hygiene pass (#3658).

Registered so :func:`teatree.loops.registry.iter_loops` discovers it and the seeded
``Loop`` row has a home (the seed/registry parity invariant). The pass itself is one
scanner job whose ``scan`` runs :func:`~teatree.core.owner_dm_sweep.run_sweep`; the
loop-table fan-out drives the hourly cadence off the DB row, like every other loop.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.loop.job_identity import _ScannerJob

DM_SWEEP_LOOP_NAME = "dm_sweep"
DM_SWEEP_DEFAULT_CADENCE_SECONDS = 3600


def _build_jobs(
    *,
    messaging: "MessagingBackend | None" = None,
    backends: "list[OverlayBackends] | None" = None,
    **_: object,
) -> "list[_ScannerJob]":
    """One global sweep job; ``messaging`` (when present) arms the owner-replied rule."""
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 (lazy import)
    from teatree.loop.scanners.dm_sweep import DmSweepScanner  # noqa: PLC0415 (lazy import)

    backend = messaging
    if backend is None and backends:
        # ``getattr``: the per-overlay tick passes real ``OverlayBackends``, but the
        # registry-coverage lane probes every ``build_jobs`` with an opaque stand-in.
        backend = next((found for entry in backends if (found := getattr(entry, "messaging", None)) is not None), None)
    return [_ScannerJob(scanner=DmSweepScanner(backend=backend), overlay="")]


MINI_LOOP = MiniLoop(
    name=DM_SWEEP_LOOP_NAME,
    default_cadence_seconds=DM_SWEEP_DEFAULT_CADENCE_SECONDS,
    build_jobs=_build_jobs,
)
