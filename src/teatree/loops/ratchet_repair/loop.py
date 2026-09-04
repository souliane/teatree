"""Reference-ratchet staleness mini-loop (#4451) — notice a loose ratchet on the core clone.

Default-OFF (``default_enabled = false`` in the seed). When enabled it reads the
maintained teatree core clone every 30m; the scanner
(:mod:`teatree.loop.scanners.ratchet_staleness`) flags pins the tree no longer
reports and the mechanical handler surfaces them with the one-command repair.

``DETERMINISTIC``: the whole tick is a YAML read plus a source walk, then a DM. It
dispatches no agent and calls no model. ``INGRESS`` because it reads the clone;
``EGRESS`` because it can DM the owner — never ``COLLEAGUE``, since the report goes
to the owner's own surface and reaches nobody else.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, LoopReach, MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

# Slower than the merge cadence on purpose: a stale pin is only introduced BY a merge,
# and the failing CI is itself a loud parallel signal, so a tighter poll buys nothing.
_CADENCE_SECONDS = 1800


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _ratchet_staleness_scanner  # noqa: PLC0415 — tick-time import
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 — deferred: loaded at fan-out, not import

    scanner = _ratchet_staleness_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="ratchet_repair",
    default_cadence_seconds=_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    declared_reach=frozenset({LoopReach.INGRESS, LoopReach.EGRESS}),
    determinism=LoopDeterminism.DETERMINISTIC,
)
