"""``outer_loop`` mini-loop registration — off the live tick (T4-PR-3).

The autoresearch outer loop is heavier and far lower-frequency than a scanner
tick, so — like the ``dream`` consolidation pass — it is marked ``off_live_tick``
and driven by its OWN cron (``t3 outer tick``), gating on the ``outer_loop``
:class:`~teatree.core.models.Loop` row's ``is_due`` / ``last_run_at`` ledger. It
is registered as a :class:`MiniLoop` so :func:`teatree.loops.registry.iter_loops`
discovers it (keeping seed/registry parity) and the statusline shows its cadence,
but ``build_jobs`` returns no scanner jobs — the tick logic
(:func:`teatree.loops.outer_loop.tick.run_tick`) is invoked directly by the cron,
not through the scanner-signal dispatch pipeline.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

OUTER_LOOP_NAME = "outer_loop"
OUTER_LOOP_LEASE_NAME = "outer-loop-tick"
OUTER_LOOP_DEFAULT_CADENCE_SECONDS = 24 * 3600  # daily; the cron drives the actual firing.
OUTER_LOOP_LEASE_SECONDS = 10 * 60


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    """No scanner jobs — the outer-loop cron invokes ``run_tick`` directly."""
    return []


MINI_LOOP = MiniLoop(
    name=OUTER_LOOP_NAME,
    default_cadence_seconds=OUTER_LOOP_DEFAULT_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    off_live_tick=True,
)
