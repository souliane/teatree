"""Shared enable + cadence gate for the mini-loop registry (#1481).

Both :class:`teatree.loops.orchestrator.Orchestrator.tick` and
:func:`teatree.loop.global_scanner_factories.build_default_jobs` decide which mini-loops
run a given tick. Before #1481 each owned its own copy of the
enable/cadence logic — the two-sources-of-truth drift the issue calls
out. :func:`elapsed_and_enabled` is the single decision both call so the
live tick and the orchestrator stay in lockstep.

The gate resolves enable/disable via :class:`LoopsConfig` (env
kill-switch, per-loop table, global, always-on) then consults the
cadence ledger: ``None`` elapsed (no marker) fires immediately so a
fresh install does not wait one window before its first dispatch.
"""

import datetime as dt
from dataclasses import dataclass

from teatree.loops.base import MiniLoop
from teatree.loops.cadence_ledger import MiniLoopMarker
from teatree.loops.config import LoopsConfig

type SkipReason = str

SKIP_DISABLED: SkipReason = "disabled"
SKIP_CADENCE: SkipReason = "cadence"


@dataclass(frozen=True, slots=True)
class GateDecision:
    should_fire: bool
    skip_reason: SkipReason | None = None


def elapsed_and_enabled(config: LoopsConfig, loop: MiniLoop, now: dt.datetime) -> GateDecision:
    if not config.is_enabled(loop):
        return GateDecision(should_fire=False, skip_reason=SKIP_DISABLED)
    elapsed = MiniLoopMarker.objects.elapsed_since(loop.name, now)
    if elapsed is not None and elapsed < config.cadence_for(loop):
        return GateDecision(should_fire=False, skip_reason=SKIP_CADENCE)
    return GateDecision(should_fire=True)
