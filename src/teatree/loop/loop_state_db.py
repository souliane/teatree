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
        logger.debug("LoopState read failed for %r — failing safe to no-hold (loop runs)", name, exc_info=True)
        return False


def loop_enabled(name: str) -> bool:
    """The single enable verdict over the DB: ``Loop.enabled`` AND not ``LoopState``-held.

    The ONE function every enable-decision site routes through — the loop tick,
    the dream cron gate, the review-claim chokepoint, and the #2650 cron mirror —
    so the four can never drift back into a tier-subset verdict (one site keying on
    ``Loop.enabled`` alone, another on ``LoopState`` alone). A loop is enabled iff
    its durable ``Loop`` row carries ``enabled=True`` AND no ``LoopState``
    pause/disable holds it.

    A missing row or ``enabled=False`` is a real, deterministic disable (``False``).
    FAIL SAFE: a genuine read error (DB unavailable, Django not configured) resolves
    to ``True`` so a hiccup never silently disables a loop — symmetric with
    :func:`loop_held_in_db`.
    """
    try:
        from teatree.core.models import Loop  # noqa: PLC0415

        row = Loop.objects.filter(name=name).only("enabled").first()
    except Exception:
        logger.debug("Loop.enabled read failed for %r — failing safe to enabled", name, exc_info=True)
        return True
    if row is None or not row.enabled:
        return False
    return not loop_held_in_db(name)


__all__ = ["loop_enabled", "loop_held_in_db"]
