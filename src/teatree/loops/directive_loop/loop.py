"""``directive_loop`` mini-loop registration — off the live tick (north-star PR-7).

The directive self-modification loop is heavy and low-frequency — like the ``dream``
and ``outer_loop`` passes — so it is marked ``off_live_tick`` and driven by the
worker's off-live-tick driver chain
(:func:`teatree.loops.off_live_tick_driver.drive_off_live_tick_loops`), which fires the
``off_tick_command`` below and gates on the ``directive_loop`` :class:`Loop` row's
``is_due`` / ``last_run_at`` ledger. It is registered as a :class:`MiniLoop` so
:func:`teatree.loops.registry.iter_loops` discovers it (keeping seed/registry parity),
but ``build_jobs`` returns no scanner jobs — the tick logic
(:func:`teatree.loops.directive_loop.tick.run_tick`) is invoked directly by the tick
command.

TRIPLE-OFF layers 1 + 2: the seeded ``Loop`` row lands DISABLED, and ``off_live_tick``
keeps it off the live work loop's fan-out entirely. These two are what keep a fresh
install inert now that the master flag ships ON (#3895); the code guards are layer 3.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, LoopReach, MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

DIRECTIVE_LOOP_NAME = "directive_loop"
DIRECTIVE_LOOP_LEASE_NAME = "directive-loop-tick"
DIRECTIVE_LOOP_DEFAULT_CADENCE_SECONDS = 3600  # hourly (#3649); the driver chain fires the actual tick.
DIRECTIVE_LOOP_LEASE_SECONDS = 10 * 60


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    """No scanner jobs — the directive-loop tick command invokes ``run_tick`` directly."""
    return []


MINI_LOOP = MiniLoop(
    name=DIRECTIVE_LOOP_NAME,
    default_cadence_seconds=DIRECTIVE_LOOP_DEFAULT_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    off_live_tick=True,
    off_tick_command=("directive", "tick"),
    declared_reach=frozenset({LoopReach.COLLEAGUE}),
    determinism=LoopDeterminism.AI,
)
