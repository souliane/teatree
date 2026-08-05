"""DB-backed LoopState control tier + the single combined enable verdict (#1913, #2584).

Two distinct facts gate a loop, and this module owns their read side. The
``Loop.enabled`` column is the durable CONFIGURED/opt-in plane (a fresh install's
seed default; a default-off loop ships ``enabled=False``). The ``LoopState`` row
is the durable runtime CONTROL plane (``t3 loop pause`` / ``disable``, the
restart-surviving 'pause everything', including the core ``dispatch`` loop), read
here via :func:`loop_held_in_db`.

:func:`loop_state_admits` is the ONE pure predicate that combines them
(configured-enabled AND not runtime-held), and it takes the mask as an ARGUMENT
rather than resolving one: resolving a mask here is what produced a THIRD variant of
the enable verdict, blind to the L0 default mode and the live-presence upgrade, which
is how ``outer_loop``'s own tick gate disagreed with the tick that drives it (#4196).
The mask is resolved ONCE, in :class:`teatree.loops.enable_verdict.EnablePlanes`, and
every enable decision — the live tick, chain membership, the off-live-tick daily gates,
the connector preflight, every observability surface — asks that one object. The review-claim chokepoint
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

from teatree.request_cache import cached_per_request

logger = logging.getLogger(__name__)


def loop_state_admits(*, configured_enabled: bool, held: bool, preset_state: bool | None, forced: bool | None) -> bool:
    """The combined enable verdict: hold > forced > preset > base config.

    Resolution, first opinion wins. A durable ``LoopState`` hold (``held``)
    always wins (the L4 emergency brake / 'pause everything') — a held loop
    never runs. Else the emergency FORCED plane (``forced``, #3248) — ``True``
    force-runs even against a preset that forces the loop off, ``False``
    force-skips, ``None`` is neutral. Else the read-time preset mask
    (``preset_state``, the L3/L2 opinion — ``True`` forces on, ``False`` off,
    ``None`` no opinion). Else L1 ``configured_enabled`` (``Loop.enabled``).

    ``preset_state`` and ``forced`` are REQUIRED at every call site — there is no
    neutral default, so the type checker structurally catches any observability
    surface that resolves a loop without both opinions (the #3159/#3248 drift
    class). The **empty-table no-op** is guaranteed by the resolvers: with no
    hold, no override, no preset, and no active schedule every loop resolves
    ``held=False``, ``forced=None``, ``preset_state=None`` — making this
    ``configured_enabled``, byte-for-byte the pre-#3159 two-plane verdict.

    The single predicate every enable-decision site applies, so the layers are
    combined identically everywhere and can never drift into a tier-subset verdict.
    """
    if held:
        return False
    if forced is not None:
        return forced
    return preset_state if preset_state is not None else configured_enabled


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


def loop_forced_in_db(name: str) -> bool | None:
    """The live emergency FORCED verdict for *name*: ``True``/``False``/``None`` (neutral).

    FAIL SAFE: any error resolves to ``None`` (no emergency opinion) so an
    unreadable DB can never silently force a loop on/off — symmetric with
    :func:`loop_held_in_db`, warning so the degraded read is observable.
    """
    try:
        from teatree.core.models import LoopState  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

        return LoopState.objects.forced_of(name)
    except Exception:
        logger.warning("LoopState forced read failed for %r — failing safe to neutral", name, exc_info=True)
        return None


def forced_loop_map() -> dict[str, bool]:
    """Every loop name with a LIVE forced value — the tick's bulk forced read.

    The bulk form of :func:`loop_forced_in_db` (mirroring :func:`held_loop_names`).
    FAIL SAFE: any read error resolves to the empty map (no forced opinions).
    """
    try:
        from teatree.core.models import LoopState  # noqa: PLC0415 (deferred, pre-app-registry — as held_loop_names)

        return LoopState.objects.forced_map()
    except Exception:
        logger.warning("LoopState bulk forced read failed — failing safe to no forced opinions", exc_info=True)
        return {}


@cached_per_request
def control_planes_in_db() -> tuple[set[str], dict[str, bool]]:
    """The (held names, live forced map) pair in ONE bulk read — the tick's single control read.

    FAIL SAFE: any read error resolves to ``(set(), {})`` (no holds, no forced
    opinions) so an unreadable DB never silently disables/forces the fleet.
    """
    try:
        from teatree.core.models import LoopState  # noqa: PLC0415 (deferred, pre-app-registry — as held_loop_names)

        return LoopState.objects.control_planes()
    except Exception:
        logger.warning("LoopState bulk control-plane read failed — failing safe to no holds/forced", exc_info=True)
        return set(), {}


__all__ = [
    "forced_loop_map",
    "held_loop_names",
    "loop_forced_in_db",
    "loop_held_in_db",
    "loop_state_admits",
]
