"""The idle-time memory-consolidation ("dreaming") mini-loop (#1933).

A low-frequency cron, decoupled from the live 12-minute work loop, that
replays recent session signal and distils it into the ``ConsolidatedMemory``
DB ledger. This package holds the MiniLoop registration (off the live tick)
and the distillation-engine seam; the cron mechanics (in-flight lease,
cadence gate, ``DreamRunMarker`` stamping) live in the ``dream`` management
command, and the staleness alarm in ``t3 doctor``.
"""
