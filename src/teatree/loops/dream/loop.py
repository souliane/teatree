"""``dream`` mini-loop — idle-time memory consolidation, off the live tick (#1933).

The dreaming consolidation pass is heavier than a scanner tick and must not
run on — or re-arm — the live 12-minute work loop (issue #1933 § 3). It is
registered as a MiniLoop so its cadence is configurable under ``[loops.dream]``
and the statusline can show its countdown, but it is marked ``off_live_tick``
so the live fan-out (:func:`teatree.loops.fanout.build_registry_jobs`) and the
:class:`teatree.loops.orchestrator.Orchestrator` skip it. The actual pass is
driven by its own low-frequency cron, the ``dream`` management command
(``t3 dream tick`` / ``t3 dream run``), which reuses the cadence ledger
(:class:`teatree.core.models.MiniLoopMarker`) and the in-flight lease
(:class:`teatree.core.models.LoopLease`).

``build_jobs`` deliberately returns no scanner jobs — the consolidation engine
is invoked directly by the cron, not through the scanner-signal dispatch
pipeline.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

DREAM_LOOP_NAME = "dream"
DREAM_LEASE_NAME = "dream-tick"
DREAM_DEFAULT_CADENCE_SECONDS = 24 * 3600  # nightly; the cron drives the actual ~04:00 firing.


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    """No scanner jobs — the dream cron invokes the engine directly."""
    return []


MINI_LOOP = MiniLoop(
    name=DREAM_LOOP_NAME,
    default_cadence_seconds=DREAM_DEFAULT_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    off_live_tick=True,
)
