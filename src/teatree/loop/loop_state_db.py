"""DB-backed LoopState control tier + the single combined enable verdict (#1913, #2584).

Two durable HUMAN interventions gate a loop, and this module owns their read side. The
``LoopState`` row is the emergency brake (``t3 loop pause`` / ``disable``, the
restart-surviving 'pause everything', including the core ``dispatch`` loop), read here via
:func:`loop_held_in_db`. The tri-state ``Loop.enabled`` column is the MANUAL override
(A3): ``None`` — the normal state — means no opinion and the preset decides, read here in
bulk via :func:`manual_override_map`.

:func:`loop_state_admits` is the ONE pure predicate that combines them with the preset,
and it takes that preset state as an ARGUMENT rather than resolving one: resolving a mask
here is what produced a THIRD variant of the enable verdict, blind to the configured
default mode, which is how ``outer_loop``'s own tick gate disagreed with the tick that
drives it (#4196).
The mask is resolved ONCE, in :class:`teatree.loops.enable_verdict.EnablePlanes`, and
every enable decision — the live tick, chain membership, the off-live-tick daily gates,
the connector preflight, every observability surface — asks that one object. The review-claim chokepoint
(:func:`teatree.loop.review_claim_signals.review_loop_enabled`) is the ONE
deliberate exception: by documented design (#79) it reads the ``LoopState`` arm
ONLY (:func:`loop_held_in_db`), never ``Loop.enabled`` — a claim-suppression
gate, not a loop-run decision. It inherits E3's direction: an unreadable brake
suppresses the claim, which costs a pass rather than a claim nobody authorised.

It is the SINGLE disable authority (loop control is ``/loops`` + the DB only;
there is no env kill-switch and no ``[loops]`` toml fallback). A ``domain``-layer
leaf depending only on :mod:`teatree.core.models` (a deferred read), so both the
orchestration tick gate and the domain-layer review-claim signals leaf may import
it downward.
"""

import logging
from typing import Final

from teatree.request_cache import cached_per_request

logger = logging.getLogger(__name__)


#: Which plane an unreadable-control error names, so the message is built from a value.
HOLD_PLANE: Final = "hold"
MANUAL_PLANE: Final = "manual override"


class ControlPlanesUnreadableError(RuntimeError):
    """A control plane could not be READ, so no loop's verdict is knowable this pass.

    Raised instead of a neutral empty answer, which is indistinguishable from a genuinely
    empty table: on that reading a database hiccup silently drops the emergency brake and a
    held destructive loop runs. The caller's correct response is to run nothing this pass and
    say so — the loop retries next tick, so loudness costs a pass, never the fleet (E2).
    """

    def __init__(self, plane: str) -> None:
        super().__init__(f"the LoopState {plane} plane could not be read")


def loop_state_admits(*, held: bool, manual: bool | None, preset_state: bool) -> bool:
    """The combined enable verdict: hold > manual override > preset.

    Resolution, first opinion wins, and it is three layers deep because that is what the
    read order (B9) says: a durable ``LoopState`` hold always wins — a held loop never
    runs. Else the tri-state MANUAL override (``Loop.enabled``) when a human set one.
    Else the active preset, which answers for every loop and so always decides.

    Both arguments are REQUIRED at every call site — there is no neutral default, so the
    type checker structurally catches an observability surface that resolves a loop
    without them.
    """
    if held:
        return False
    return manual if manual is not None else preset_state


def loop_held_in_db(name: str) -> bool:
    """Is *name* explicitly paused/disabled by a durable ``LoopState`` row?

    Returns ``True`` when a ``PAUSED`` / ``DISABLED`` row forces a skip (the
    restart-surviving 'pause everything', including the core ``dispatch`` loop)
    and ``False`` when no DB hold applies (no row, or an ``ENABLED`` row), so an
    empty table is a provable no-op. This is the single disable authority — loop
    control is ``/loops`` + the DB only.

    FAILS CLOSED (E3): any error (DB unavailable, Django not configured, model
    unimportable) resolves to ``True`` — the hold stands. An unreadable row is not
    evidence of no hold, and reading it as one meant a database hiccup silently dropped
    the emergency brake and ran a held destructive loop. The refusal logs at ERROR: a
    brake this box cannot read is an incident, not a degraded read.
    """
    try:
        from teatree.core.models import LoopState  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

        return not LoopState.objects.is_runnable(name)
    except Exception:
        logger.exception("LoopState read failed for %r — failing CLOSED, the hold stands", name)
        return True


def held_loop_names() -> set[str]:
    """Every loop name a durable ``PAUSED`` / ``DISABLED`` row holds — the tick's bulk hold read.

    The set form of :func:`loop_held_in_db` the loop-table fan-out consumes once
    per tick (``name in held``) instead of a per-loop query (#2584 N+1). A read error
    RAISES :class:`ControlPlanesUnreadableError`: the empty set it used to return is
    what a box with no holds also returns, so the caller could not tell the brake was
    unreadable from the brake being off.
    """
    try:
        from teatree.core.models import LoopState  # noqa: PLC0415 (deferred, pre-app-registry — as loop_held_in_db)

        return LoopState.objects.held_names()
    except Exception as error:
        logger.exception("LoopState bulk hold read failed — no verdict is knowable this pass")
        raise ControlPlanesUnreadableError(HOLD_PLANE) from error


@cached_per_request
def manual_override_map() -> dict[str, bool]:
    """Every loop carrying a MANUAL override, in ONE bulk read (A3).

    A read error RAISES :class:`ControlPlanesUnreadableError` rather than answering ``{}``,
    which is exactly what a fleet with no overrides answers — so the tick could not tell
    "nobody has overridden anything" from "the manual layer is unreadable".
    """
    try:
        from teatree.core.models import Loop  # noqa: PLC0415 (deferred, pre-app-registry — as held_loop_names)

        return {name: runs for name, runs in Loop.objects.values_list("name", "enabled") if isinstance(runs, bool)}
    except Exception as error:
        logger.exception("manual-override read failed — no verdict is knowable this pass")
        raise ControlPlanesUnreadableError(MANUAL_PLANE) from error


__all__ = [
    "ControlPlanesUnreadableError",
    "held_loop_names",
    "loop_held_in_db",
    "loop_state_admits",
    "manual_override_map",
]
