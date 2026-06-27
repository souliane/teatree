"""DB-backed LoopState control tier, read by loop NAME (#1913).

The single ORM read of the per-loop control plane both the tick gate
(:meth:`teatree.loops.config.LoopsConfig.is_enabled`) and the review-claim
chokepoint (:mod:`teatree.loop.review_claim_signals.review_loop_enabled`)
consult — so the "is this loop durably paused/disabled?" answer cannot drift
between them. It is the SINGLE disable authority (loop control is ``/loops`` +
the DB only; there is no env kill-switch). A ``domain``-layer leaf depending
only on :mod:`teatree.core.models` (a deferred, fail-safe read), so both the
orchestration tick gate and the domain-layer review-claim signals leaf may
import it downward.
"""

import logging

logger = logging.getLogger(__name__)


def loop_held_in_db(name: str) -> bool:
    """Is *name* explicitly paused/disabled by a durable ``LoopState`` row?

    Returns ``True`` when a ``PAUSED`` / ``DISABLED`` row forces a skip (the
    restart-surviving 'pause everything', including the core ``dispatch`` loop)
    and ``False`` when no DB hold applies (no row, or an ``ENABLED`` row), so an
    empty table is a provable no-op. This is the single disable authority — loop
    control is ``/loops`` + the DB only.

    FAIL SAFE: any error (DB unavailable, Django not configured, model
    unimportable) resolves to ``False`` (no hold) so an unreadable database can
    never silently disable a loop.
    """
    try:
        from teatree.core.models import LoopState  # noqa: PLC0415

        return not LoopState.objects.is_runnable(name)
    except Exception:
        logger.debug("LoopState read failed for %r — falling through to env kill-switch", name, exc_info=True)
        return False


__all__ = ["loop_held_in_db"]
