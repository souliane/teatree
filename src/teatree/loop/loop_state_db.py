"""DB-backed LoopState control tier + the single combined enable verdict (#1913, #2584).

Two distinct facts gate a loop, and this module owns their read side. The
``Loop.enabled`` column is the durable CONFIGURED/opt-in plane (a fresh install's
seed default; a default-off loop ships ``enabled=False``). The ``LoopState`` row
is the durable runtime CONTROL plane (``t3 loop pause`` / ``disable``, the
restart-surviving 'pause everything', including the core ``dispatch`` loop), read
here via :func:`loop_held_in_db`.

:func:`loop_state_admits` is the ONE pure predicate that combines them
(configured-enabled AND not runtime-held). Every enable-decision site resolves a
loop through it, so the verdict can never drift into a tier-subset: the standalone
:func:`loop_enabled` single-lookup (the off-live-tick daily loop gates —
``directive``/``outer``/``dream`` tick commands + the per-loop connector
preflight) and the live loop-table tick (:func:`teatree.loops.loop_table._loop_admitted`,
which applies the same predicate over its already-bulk-loaded ``Loop`` rows + one
bulk ``LoopState`` read) both call it. The timer-chain admission reuses the tick's
verdict. The review-claim chokepoint
(:func:`teatree.loop.review_claim_signals.review_loop_enabled`) is the ONE
deliberate exception: by documented design (#79) it reads the ``LoopState`` arm
ONLY (:func:`loop_held_in_db`), never ``Loop.enabled`` — a fail-open
claim-suppression gate, not a loop-run decision.

It is the SINGLE disable authority (loop control is ``/loops`` + the DB only;
there is no env kill-switch and no ``[loops]`` toml fallback). A ``domain``-layer
leaf depending only on :mod:`teatree.core.models` (a deferred, fail-safe read),
so both the orchestration tick gate and the domain-layer review-claim signals
leaf may import it downward.
"""

import logging

from teatree.loop.preset_resolution import resolve_preset_state

logger = logging.getLogger(__name__)


def loop_state_admits(*, configured_enabled: bool, held: bool, preset_state: bool | None) -> bool:
    """The combined enable verdict: NOT runtime-HELD, then the preset mask over base config.

    Resolution: a durable ``LoopState`` hold (``held``) always wins (the L4
    emergency brake). Otherwise the read-time preset mask decides — ``preset_state``
    is the L3/L2 opinion from :func:`teatree.loop.preset_resolution.resolve_preset_state`
    (``True`` forces on, ``False`` forces off), and ``None`` means "no preset opinion",
    falling through to L1 ``configured_enabled`` (``Loop.enabled``).

    ``preset_state`` is REQUIRED at every call site — there is no neutral default,
    so the type checker structurally catches any observability surface that resolves
    a loop without the preset opinion (the #3159 drift class). The **empty-table
    no-op** is guaranteed instead by the resolver: with no preset, no override, and
    no active schedule every loop resolves ``preset_state=None``, making this
    ``not held and configured_enabled`` — byte-for-byte the pre-#3159 two-plane verdict.

    The single predicate both :func:`loop_enabled` (single-lookup) and the live
    loop-table tick apply, so the layers are combined identically everywhere and
    can never drift into a tier-subset verdict.
    """
    return not held and (preset_state if preset_state is not None else configured_enabled)


def loop_held_in_db(name: str) -> bool:
    """Is *name* explicitly paused/disabled by a durable ``LoopState`` row?

    Returns ``True`` when a ``PAUSED`` / ``DISABLED`` row forces a skip (the
    restart-surviving 'pause everything', including the core ``dispatch`` loop)
    and ``False`` when no DB hold applies (no row, or an ``ENABLED`` row), so an
    empty table is a provable no-op. This is the single disable authority — loop
    control is ``/loops`` + the DB only.

    FAIL SAFE: any error (DB unavailable, Django not configured, model
    unimportable) resolves to ``False`` (no hold) so an unreadable database can
    never silently disable a loop. The swallow logs at WARNING — the global
    kill-switch fails CLOSED on a read error, so this symmetric per-loop
    fail-OPEN must be observable, not whispered at ``debug`` (holistic 3c#5): a
    loop silently kept running past a durable PAUSE/DISABLE is exactly the
    false-quiet class the fleet-safety work exists to surface.
    """
    try:
        from teatree.core.models import LoopState  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

        return not LoopState.objects.is_runnable(name)
    except Exception:
        logger.warning("LoopState read failed for %r — failing safe to no-hold (loop runs)", name, exc_info=True)
        return False


def held_loop_names() -> set[str]:
    """Every loop name a durable ``PAUSED`` / ``DISABLED`` row holds — the tick's bulk hold read.

    The set form of :func:`loop_held_in_db` the loop-table fan-out consumes once
    per tick (``name in held``) instead of a per-loop query (#2584 N+1). FAIL SAFE
    symmetric with :func:`loop_held_in_db`: any read error resolves to the empty
    set (no holds) so an unreadable DB can never silently disable every loop, and
    it WARNS so the degraded read is observable.
    """
    try:
        from teatree.core.models import LoopState  # noqa: PLC0415 (deferred, pre-app-registry — as loop_held_in_db)

        return LoopState.objects.held_names()
    except Exception:
        logger.warning("LoopState bulk read failed — failing safe to no-holds (loops run)", exc_info=True)
        return set()


def loop_enabled(name: str) -> bool:
    """The single-lookup combined enable verdict: ``Loop.enabled`` AND not ``LoopState``-held.

    The single-query form of :func:`loop_state_admits` the off-live-tick daily
    loop gates use (``directive``/``outer``/``dream`` tick commands + the per-loop
    connector preflight): a loop is enabled iff its durable ``Loop`` row carries
    ``enabled=True`` AND no ``LoopState`` pause/disable holds it. The live
    loop-table tick reaches the SAME verdict through the same predicate over
    bulk-loaded rows, so no site drifts into a tier-subset.

    A missing row or ``enabled=False`` is a real, deterministic disable (``False``).
    The read-time preset mask (L3 override / L2 schedule slot) resolves through the
    SAME predicate, so the off-live-tick daily gates and the connector preflight
    honour a preset without a code change at each call site.
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
    if row is None:
        return False
    return loop_state_admits(
        configured_enabled=row.enabled, held=loop_held_in_db(name), preset_state=resolve_preset_state(name)
    )


__all__ = ["held_loop_names", "loop_enabled", "loop_held_in_db", "loop_state_admits"]
