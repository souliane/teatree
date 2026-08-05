"""The ONE enable-verdict seam — chain membership and per-fire admission share it (#4185).

Chain MEMBERSHIP (:mod:`teatree.loops.chain_membership`) and the per-FIRE admission the
live tick gates on (:mod:`teatree.loops.loop_table`) are two readings of ONE decision:
hold > forced > mode mask > ``Loop.enabled``. Nothing here is a second opinion — the
membership set and the tick's own gate call the same object, so they cannot answer
differently.

They CAN when each resolves the mask for itself, and they did. Membership read the L3/L2
preset layer (:func:`teatree.loop.preset_resolution.resolve_active_preset`), which stops
at ``None`` when neither an override nor a schedule slot governs; the tick read
:func:`teatree.core.mode_resolution.resolve_active_mode`, which continues to the L0
``default_mode`` row and then applies the live-presence upgrade. At a scheduled away slot
with a fresh keystroke the tick therefore admitted loops membership called non-members,
and :func:`teatree.loops.timer_reconciler.ensure_loop_timers` DELETED the READY timers
driving them — the loops stopped ticking exactly while the owner was at the keyboard.

:class:`EnablePlanes` holds every input the verdict needs, read once, and answers both
questions: :meth:`~EnablePlanes.verdict_for` for the observability surfaces and
:meth:`~EnablePlanes.refusal` for the tick's operator-actionable reason. The boolean and
the reason come off the same call, so a refusal can never name an arm that did not decide
it.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from teatree.core.mode_resolution import ResolvedMode, resolve_active_mode
from teatree.loop.loop_state_db import control_planes_in_db, loop_state_admits
from teatree.request_cache import cached_per_request

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoopVerdict:
    """One loop's effective run verdict and the layer that decided it."""

    name: str
    admitted: bool
    #: ``hold`` | ``forced`` | the resolved mode's own source (``override`` / ``schedule``
    #: / ``live`` / ``default``) when the mask holds an opinion | ``base``.
    layer: str
    detail: str


@dataclass(frozen=True, slots=True)
class EnablePlanes:
    """The resolved mode plus the bulk-read hold/forced planes — the verdict's whole input.

    Resolved once per tick (or once per observability read) so a fan-out of N loops
    issues those reads once rather than per loop, and so every loop in one pass is
    judged against ONE instant's mode.
    """

    resolved: ResolvedMode
    held: set[str]
    forced: dict[str, bool]

    @classmethod
    def resolve(cls, now: dt.datetime | None = None) -> "EnablePlanes":
        held, forced = control_planes_in_db()
        return cls(resolved=resolve_active_mode(now), held=held, forced=forced)

    def admits(self, name: str, *, configured_enabled: bool) -> bool:
        """The verdict AT THIS INSTANT — what the tick gates each individual fire on."""
        return self._admits_under(name, mask=self.resolved.state_for(name), configured_enabled=configured_enabled)

    def admits_any_mask(self, name: str, *, configured_enabled: bool) -> bool:
        """The verdict over the presence-INVARIANT closure — what a PERSISTED decision needs.

        :meth:`admits` answers for one instant. A ``loop_timer`` chain is built once and
        fires later, so it needs the verdict for every instant in between, and the two
        errors are not symmetric: a member the tick refuses costs one ``skipped`` no-op
        (the successor is queued before the gate, so the chain self-corrects the moment
        admission flips true), while an admitted loop that is not a member does not run at
        all AND has its existing timer DELETED by
        :func:`teatree.loops.timer_reconciler.ensure_loop_timers`. Pruning is the
        destructive direction, so membership must cover every verdict reachable while the
        chain lives.

        Exactly one arm can flip inside that window with nothing to hook: the live-presence
        upgrade, raised by a keystroke and lowered by the mere absence of one. Every other
        arm moves only on a durable write (hold, forced, ``ModeOverride``, ``default_mode``)
        or a schedule boundary, each of which has a chokepoint. So this closes over the
        presence axis ALONE — the mode's mask OR its
        :attr:`~teatree.core.mode_resolution.ResolvedMode.presence_alternate` — and reads
        every other plane exactly as :meth:`admits` does. A ``LoopState`` hold still loses
        its chain; a loop BOTH sides of the flip mask off still loses its chain.
        """
        if self.admits(name, configured_enabled=configured_enabled):
            return True
        alternate = self.resolved.presence_alternate
        if alternate is None:
            return False
        return self._admits_under(name, mask=alternate.state_for(name), configured_enabled=configured_enabled)

    def _admits_under(self, name: str, *, mask: bool | None, configured_enabled: bool) -> bool:
        return loop_state_admits(
            configured_enabled=configured_enabled,
            held=name in self.held,
            preset_state=mask,
            forced=self.forced.get(name),
        )

    def verdict_for(self, name: str, *, configured_enabled: bool) -> LoopVerdict:
        """The verdict plus the layer that decided it, mirroring the resolution order."""
        admitted = self.admits(name, configured_enabled=configured_enabled)
        if name in self.held:
            return LoopVerdict(name=name, admitted=admitted, layer="hold", detail="LoopState hold")
        forced = self.forced.get(name)
        if forced is not None:
            state = "on" if forced else "off"
            return LoopVerdict(name=name, admitted=admitted, layer="forced", detail=f"override {state}")
        if self.resolved.state_for(name) is not None:
            return LoopVerdict(name=name, admitted=admitted, layer=self.resolved.source, detail=self.resolved.reason)
        return LoopVerdict(name=name, admitted=admitted, layer="base", detail="Loop.enabled")

    def refusal(self, name: str, *, configured_enabled: bool) -> str:
        """Which enable plane refused *name* — the empty string when none did.

        Derived from :meth:`admits`, then walking the planes only to NAME the arm that
        said no, so the printed reason can never disagree with the decision. PURE over
        the already-bulk-loaded planes — it issues no query of its own.
        """
        if self.admits(name, configured_enabled=configured_enabled):
            return ""
        if name in self.held:
            return f"held by a durable LoopState pause/disable (`t3 loop resume {name} --emergency` lifts it)"
        if self.forced.get(name) is False:
            return (
                f"forced OFF by a LoopState override — `t3 loop loop-state {name}` shows the "
                f"recorded reason, `t3 loop override {name} clear` lifts it"
            )
        if self.resolved.state_for(name) is False:
            return f"masked off by the active preset/schedule ({self.resolved.name!r})"
        return f"disabled — Loop.enabled is false (`t3 loop enable {name} --emergency` re-enables it)"


@cached_per_request
def effective_verdicts(now: dt.datetime | None = None) -> list[LoopVerdict]:
    """The NARROW, instant verdict + deciding layer for every ``Loop`` row, sorted by name.

    What the tick gates a fire on, so this is what every observability surface reports —
    "will this loop run now". Chain membership is the wider
    :func:`membership_loop_names`; the two are named apart deliberately, because reporting
    the membership set here would tell an operator a masked-off loop is running.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    planes = EnablePlanes.resolve(now)
    verdicts = [planes.verdict_for(row.name, configured_enabled=row.enabled) for row in Loop.objects.all()]
    return sorted(verdicts, key=lambda verdict: verdict.name)


def loop_admits(name: str, now: dt.datetime | None = None) -> bool:
    """The instant verdict for ONE loop — the single-lookup form of :meth:`EnablePlanes.admits`.

    What the off-live-tick daily gates (``directive`` / ``dream`` / ``outer`` tick
    commands) and the per-loop connector preflight ask. It used to live in
    :mod:`teatree.loop.loop_state_db` and resolve its own mask through the L3/L2 preset
    layer — a THIRD variant of the verdict, blind to the L0 default mode and the presence
    upgrade, so ``outer_loop``'s own gate could refuse a tick the fleet's verdict admitted
    (#4196). A missing ``Loop`` row is a real, deterministic disable.

    FAIL SAFE, unchanged: a genuine read error resolves to ``True`` so a DB hiccup never
    silently disables a loop, and it WARNS so the degraded read is observable.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    try:
        row = Loop.objects.filter(name=name).only("enabled").first()
        if row is None:
            return False
        return EnablePlanes.resolve(now).admits(name, configured_enabled=row.enabled)
    except Exception:
        logger.warning("enable-verdict read failed for %r — failing safe to enabled", name, exc_info=True)
        return True


@cached_per_request
def membership_loop_names(now: dt.datetime | None = None) -> set[str]:
    """Every ``Loop`` row the presence-invariant closure admits — the chain-membership read.

    The ONLY caller of :meth:`EnablePlanes.admits_any_mask`; everything else asks
    :meth:`EnablePlanes.admits`. :func:`teatree.loops.chain_membership.timer_chain_loop_names`
    intersects this with the live-tick registry.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    planes = EnablePlanes.resolve(now)
    return {row.name for row in Loop.objects.all() if planes.admits_any_mask(row.name, configured_enabled=row.enabled)}


__all__ = ["EnablePlanes", "LoopVerdict", "effective_verdicts", "loop_admits", "membership_loop_names"]
