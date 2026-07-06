"""``directive_loop`` mini-loop registration — off the live tick (north-star PR-7).

The directive self-modification loop is heavy and low-frequency — like the ``dream``
and ``outer_loop`` passes — so it is marked ``off_live_tick`` and driven by its OWN
cron (``t3 directive tick``), gating on the ``directive_loop`` :class:`Loop` row's
``is_due`` / ``last_run_at`` ledger. It is registered as a :class:`MiniLoop` so
:func:`teatree.loops.registry.iter_loops` discovers it (keeping seed/registry parity),
but ``build_jobs`` returns no scanner jobs — the tick logic
(:func:`teatree.loops.directive_loop.tick.run_tick`) is invoked directly by the cron.

QUADRUPLE-OFF layer 2 + 3: the seeded ``Loop`` row lands DISABLED, and ``off_live_tick``
keeps it off the live work loop's fan-out entirely.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

DIRECTIVE_LOOP_NAME = "directive_loop"
DIRECTIVE_LOOP_LEASE_NAME = "directive-loop-tick"
DIRECTIVE_LOOP_DEFAULT_CADENCE_SECONDS = 24 * 3600  # daily; the cron drives the actual firing.
DIRECTIVE_LOOP_LEASE_SECONDS = 10 * 60


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    """No scanner jobs — the directive-loop cron invokes ``run_tick`` directly."""
    return []


MINI_LOOP = MiniLoop(
    name=DIRECTIVE_LOOP_NAME,
    default_cadence_seconds=DIRECTIVE_LOOP_DEFAULT_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    off_live_tick=True,
)
