"""Memory-skim mini-loop — the weekly cadence directive 32 asked for.

The scanner (:mod:`teatree.loop.scanners.memory_skim`) gates itself on an ISO-week
dedupe marker, so this loop's outer cadence is a FLOOR: checking more often than
weekly cannot produce a second question, and slowing it past a week would starve
the inner cadence.

Ships disabled (``default_enabled`` is absent from its ``[loops.memory_skim]``
seed entry) and the ``off``/``low-token`` presets mask it like every other loop —
an operator enables it deliberately.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

_WEEKLY = 604800


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 deferred: needed only at fan-out
    from teatree.loop.scanners.memory_skim import MemorySkimScanner  # noqa: PLC0415 deferred: cycle-break

    return [_ScannerJob(scanner=MemorySkimScanner(), overlay="")]


MINI_LOOP = MiniLoop(
    name="memory_skim",
    default_cadence_seconds=_WEEKLY,
    cadence_is_floor=True,
    build_jobs=_build_jobs,
    declared_reach=frozenset(),
    determinism=LoopDeterminism.DETERMINISTIC,
)
