"""The ONE enable-verdict seam — chain membership and per-fire admission share it (#4185).

Chain MEMBERSHIP (:mod:`teatree.loops.chain_membership`) and the per-FIRE admission the
live tick gates on (:mod:`teatree.loops.loop_table`) are two readings of ONE decision:
hold > manual override > preset. Nothing here is a second opinion — the
membership set and the tick's own gate call the same object, so they cannot answer
differently.

They CAN when each resolves the mask for itself, and they did. Membership read the
override/schedule layer (:func:`teatree.loop.preset_resolution.resolve_active_preset`),
which stops at ``None`` when neither governs; the tick read
:func:`teatree.core.mode_resolution.resolve_active_mode`, which continues to the
``default_mode`` row — so the two answered differently and
:func:`teatree.loops.timer_reconciler.ensure_loop_timers` DELETED the READY timers
driving loops the tick was admitting.

:class:`EnablePlanes` holds every input the verdict needs, read once, and answers both
questions: :meth:`~EnablePlanes.verdict_for` for the observability surfaces and
:meth:`~EnablePlanes.refusal` for the tick's operator-actionable reason. The boolean and
the reason come off the same call, so a refusal can never name an arm that did not decide
it.
"""

import datetime as dt
import enum
import logging
from dataclasses import dataclass

from teatree.core.mode_resolution import ResolvedMode, resolve_active_mode
from teatree.loop.loop_state_db import (
    ControlPlanesUnreadableError,
    held_loop_names,
    loop_state_admits,
    manual_override_map,
)
from teatree.request_cache import cached_per_request

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoopVerdict:
    """One loop's effective run verdict and the layer that decided it."""

    name: str
    admitted: bool
    #: ``hold`` | ``manual`` | the resolved mode's own source (``override`` / ``schedule``
    #: / ``default``).
    layer: str
    detail: str


@dataclass(frozen=True, slots=True)
class EnablePlanes:
    """The resolved mode plus the bulk-read hold and manual layers — the verdict's whole input.

    Resolved once per tick (or once per observability read) so a fan-out of N loops
    issues those reads once rather than per loop, and so every loop in one pass is
    judged against ONE instant's mode.
    """

    resolved: ResolvedMode
    held: set[str]
    manual: dict[str, bool]

    @classmethod
    def resolve(cls, now: dt.datetime | None = None) -> "EnablePlanes":
        return cls(resolved=resolve_active_mode(now), held=held_loop_names(), manual=manual_override_map())

    def admits(self, name: str) -> bool:
        """The verdict — what the tick gates each individual fire on, and what a chain persists.

        Every arm moves only on a durable write (hold, manual override, ``ModeOverride``,
        ``default_mode``) or a schedule boundary, each with a chokepoint — so a decision
        taken now stays answerable later, and membership needs no wider closure.
        """
        return loop_state_admits(
            held=name in self.held,
            manual=self.manual.get(name),
            preset_state=self.resolved.state_for(name),
        )

    def verdict_for(self, name: str, *, reason: str = "") -> LoopVerdict:
        """The verdict plus the layer that decided it, mirroring the resolution order."""
        admitted = self.admits(name)
        if name in self.held:
            return LoopVerdict(name=name, admitted=admitted, layer="hold", detail="LoopState hold")
        if self.manual.get(name) is not None:
            detail = f"manual override — {reason}" if reason else "manual override"
            return LoopVerdict(name=name, admitted=admitted, layer="manual", detail=detail)
        return LoopVerdict(name=name, admitted=admitted, layer=self.resolved.source, detail=self.resolved.reason)

    def refusal(self, name: str) -> str:
        """Which enable plane refused *name* — the empty string when none did.

        Derived from :meth:`admits`, then walking the planes only to NAME the arm that
        said no, so the printed reason can never disagree with the decision. PURE over
        the already-bulk-loaded planes — it issues no query of its own.
        """
        if self.admits(name):
            return ""
        if name in self.held:
            return f"held by a durable LoopState pause/disable (`t3 loop resume {name} --emergency` lifts it)"
        if self.manual.get(name) is False:
            return (
                f"forced OFF by a manual override — `t3 loop loop-state {name}` shows the "
                f"recorded reason, `t3 loop override {name} clear` lifts it"
            )
        return f"masked off by the active preset ({self.resolved.name!r})"


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
    verdicts = [planes.verdict_for(row.name, reason=row.override_reason) for row in Loop.objects.all()]
    return sorted(verdicts, key=lambda verdict: verdict.name)


def loop_admits(name: str, now: dt.datetime | None = None) -> bool:
    """The instant verdict for ONE loop — the single-lookup form of :meth:`EnablePlanes.admits`.

    What the off-live-tick daily gates (``directive`` / ``dream`` / ``outer`` tick
    commands) and the per-loop connector preflight ask. It used to live in
    :mod:`teatree.loop.loop_state_db` and resolve its own mask through the
    override/schedule layer — a THIRD variant of the verdict, blind to the configured
    default mode, so ``outer_loop``'s own gate could refuse a tick the fleet's verdict
    admitted (#4196). A missing ``Loop`` row is a real, deterministic disable.

    A ``Loop``-side read error still resolves to ``True`` so a DB hiccup never silently
    disables a loop, and it WARNS. An unreadable HOLD plane
    (:class:`~teatree.loop.loop_state_db.ControlPlanesUnreadableError`) is the opposite
    case and propagates: swallowing it here would turn E3's loud refusal back into the
    silent "everything runs" it exists to prevent.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    try:
        if not Loop.objects.filter(name=name).exists():
            return False
        return EnablePlanes.resolve(now).admits(name)
    except ControlPlanesUnreadableError:
        raise
    except Exception:
        logger.warning("enable-verdict read failed for %r — failing safe to enabled", name, exc_info=True)
        return True


@cached_per_request
def membership_loop_names(now: dt.datetime | None = None) -> set[str]:
    """Every ``Loop`` row the verdict admits — the chain-membership read.

    :func:`teatree.loops.chain_membership.timer_chain_loop_names` intersects this with
    the live-tick registry.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    planes = EnablePlanes.resolve(now)
    return {name for name in Loop.objects.values_list("name", flat=True) if planes.admits(name)}


class FleetAdmission(enum.Enum):
    """Whether the layering admits ANY loop — the whole fleet's run/stop verdict.

    ``UNREADABLE`` is a read that RAISED, kept distinct from ``NONE`` so the worker can
    crash-restart on "cannot confirm" while a genuine all-off preset still stops cleanly.
    """

    ADMITS = "admits"
    NONE = "none"
    UNREADABLE = "unreadable"


def read_fleet_admission(now: dt.datetime | None = None) -> FleetAdmission:
    """The fleet verdict: does the active preset (with holds and overrides) admit a loop?

    This is the stop condition ``loop_runner_enabled`` used to be, moved onto the layer
    that already decides every loop — so a preset admitting nothing IS the fleet stopping,
    with no second surface that can disagree with it.
    """
    try:
        return FleetAdmission.ADMITS if membership_loop_names(now) else FleetAdmission.NONE
    except Exception:
        logger.warning("enable-plane read failed — cannot confirm whether the fleet admits work", exc_info=True)
        return FleetAdmission.UNREADABLE


def fleet_admits_work(now: dt.datetime | None = None) -> bool:
    """Whether the fleet admits a loop (fail-safe: anything but ADMITS is False).

    The boolean every work-driving chain fire gates on. An unreadable plane must not keep
    a chain alive; the worker consults :func:`read_fleet_admission` directly so it can tell
    a deliberate stop from a plane it could not read.
    """
    return read_fleet_admission(now) is FleetAdmission.ADMITS


def fleet_admission_refusal(now: dt.datetime | None = None) -> str:
    """Why the fleet admits no work, or ``""`` — naming a deliberate stop apart from an unreadable plane."""
    match read_fleet_admission(now):
        case FleetAdmission.NONE:
            return "the active preset admits no loop (`t3 loop preset show` names the posture)"
        case FleetAdmission.UNREADABLE:
            return "the fleet admission verdict is unreadable, so no work is admitted"
        case _:
            return ""


__all__ = [
    "EnablePlanes",
    "FleetAdmission",
    "LoopVerdict",
    "effective_verdicts",
    "fleet_admission_refusal",
    "fleet_admits_work",
    "loop_admits",
    "membership_loop_names",
    "read_fleet_admission",
]
