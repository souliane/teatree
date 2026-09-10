"""First-party skill-pin refresh mini-loop (#4677) — bump a pin its source moved past.

Default-OFF (``default_enabled = false`` in the seed). When enabled it reads the
maintained teatree core clone's ``apm.yml``; the scanner
(:mod:`teatree.loop.scanners.skill_pin_refresh`) flags each FIRST-PARTY source whose
head has left the declared pin and the mechanical handler opens the bump PR. A
third-party source is never fetched and never bumped — auto-pulling unreviewed upstream
skill code on a timer is exactly what pinning exists to prevent.

``DETERMINISTIC``: the tick is a manifest read, a ``git ls-remote`` per first-party
source, then a branch and a PR. It dispatches no agent and calls no model. ``INGRESS``
because it reads the sources; ``EGRESS`` because it pushes a branch, opens a PR on the
owner's own repo, and can DM the owner — never ``COLLEAGUE``, since both surfaces are
the owner's own.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, LoopReach, MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

# Six-hourly: a pin only moves when the source repo merges something, and a bump PR
# waiting a few hours costs nothing while a tighter poll is one network read per source
# per tick for an answer that rarely changes.
_CADENCE_SECONDS = 21600


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _skill_pin_refresh_scanner  # noqa: PLC0415 — tick-time import
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 — deferred: loaded at fan-out, not import

    scanner = _skill_pin_refresh_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="skill_pin_refresh",
    default_cadence_seconds=_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    declared_reach=frozenset({LoopReach.INGRESS, LoopReach.EGRESS}),
    determinism=LoopDeterminism.DETERMINISTIC,
)
