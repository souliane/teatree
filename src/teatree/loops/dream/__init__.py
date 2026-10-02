"""The idle-time memory-consolidation ("dreaming") mini-loop (#1933).

A low-frequency pass, decoupled from the live 12-minute work loop, that
replays recent session signal and distils it into the ``ConsolidatedMemory``
DB ledger. This package holds the MiniLoop registration (off the live tick, with
the ``off_tick_command`` the worker's off-live-tick driver chain fires) and the
distillation-engine seam; the tick mechanics (in-flight lease, cadence gate,
``DreamRunMarker`` stamping) live in the ``dream`` management command, and the
staleness alarm in ``t3 doctor``.
"""
