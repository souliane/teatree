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

The lease TTL is ``DREAM_LEASE_SECONDS``, sized above ``DREAM_PASS_BUDGET_SECONDS``
— the wall-clock budget cap for one consolidation pass — rather than the
``LoopLease.acquire`` 120s default. The pass is by design heavier than a scanner
tick (#1933 §3), so a default-leased pass running longer than 2min would silently
lose its lease mid-run and let a concurrent ``tick``/``run`` win the expired-lease
CAS — the overlap the "no two overlapping passes" invariant forbids. Matching the
TTL to the budget keeps the invariant true for the whole pass.

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
DREAM_PASS_BUDGET_SECONDS = 30 * 60
DREAM_LEASE_SECONDS = DREAM_PASS_BUDGET_SECONDS + 5 * 60


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    """No scanner jobs — the dream cron invokes the engine directly."""
    return []


MINI_LOOP = MiniLoop(
    name=DREAM_LOOP_NAME,
    default_cadence_seconds=DREAM_DEFAULT_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    off_live_tick=True,
)
