"""Pure per-loop / mini-loop chunk formatters for the dedicated loop line.

Split out of :mod:`teatree.loop.statusline_loops` so that module stays a thin
orchestrator (DB-read seams, injected readers, the ``live_loops_anchor``
assembly) and this one owns the presentation concern: turning a loop name plus a
timing into the ``<name> <next-tick>`` chunk the loop line renders, colored by
recency, plus the two chunk-list builders the line composes (the per-loop lease
list and the due-soon mini-loop list).

This module is a **leaf**: it imports only the ANSI palette and the per-loop
scoping predicates, never :mod:`teatree.loop.statusline_loops` itself. Everything
it needs from the orchestrator — the live leases, the driver map, the resolved
override set, and the per-loop cadence resolver — is passed in as an argument, so
there is no import cycle and the orchestrator's DB / cadence seams stay the single
place tests stub.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from teatree.loop.loop_scoping import (
    is_per_loop_owner_slot,
    is_transient_tick_mutex,
    loop_is_actively_ticking,
    per_loop_chunk_visible,
    per_loop_loop_name,
)
from teatree.loop.statusline_palette import (
    _ANSI_DIM,
    _ANSI_GREEN,
    _ANSI_RED,
    _ANSI_YELLOW,
    _RECENCY_GREEN_FRACTION,
    _RECENCY_YELLOW_FRACTION,
    _SECONDS_PER_MINUTE,
)

#: A loop is due (and so survives the #3248 per-loop-lease collapse) when its next
#: fire is now or within this horizon — the same window the ``due:`` mini-loop
#: section applies, so the two due views stay consistent.
_DUE_SOON_SECONDS = 300

#: Resolve a loop name to its cadence in seconds. Injected by the orchestrator
#: (``statusline_loops._cadence_for_loop``) so this leaf never reads config and the
#: cadence seam stays the single place tests stub.
type CadenceResolver = Callable[[str], int]


@dataclass(frozen=True, slots=True)
class LeaseRenderContext:
    """The resolved inputs :func:`lease_chunks` renders a live lease against.

    Bundles the four DB / config reads the orchestrator resolves for the whole
    batch — the driver map, this session's owned ``loop:<name>`` slots, the
    manually-overridden set (``None`` = no #3248 collapse), and the per-loop
    cadence resolver — so the renderer takes one typed context rather than a wide
    parameter list.
    """

    drivers: dict[str, tuple[str, str]]
    owned_per_loop: set[str] | None
    overridden: set[str] | None
    cadence_of: CadenceResolver


def _seconds_until(next_fire_at: datetime) -> float:
    """Return seconds from now until *next_fire_at* (negative once overdue)."""
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    return (next_fire_at - timezone.now()).total_seconds()


def _relative_minutes(next_fire_at: datetime) -> str:
    """Return the relative whole-minute countdown to *next_fire_at* (``11m`` / ``due``).

    ``due`` once the instant is in the past — the loop fires on the
    orchestrator's next tick. Always derived from the live clock at render
    time so the value counts down across successive renders rather than
    freezing on a cached string.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    delta_seconds = int((next_fire_at - timezone.now()).total_seconds())
    if delta_seconds <= 0:
        return "due"
    minutes = max(1, round(delta_seconds / _SECONDS_PER_MINUTE))
    return f"{minutes}m"


def _short_loop_name(name: str) -> str:
    """Strip the ``loop-`` prefix so ``loop-my-prs`` reads as ``my-prs``."""
    return name.removeprefix("loop-")


def _colorize_chunk(text: str, color: str, *, colorize: bool) -> str:
    """Wrap *text* in *color*, resetting to the line's dim baseline (not full reset).

    The whole loop line is wrapped in :data:`_ANSI_DIM` by
    :func:`teatree.loop.statusline_render.render`, so a colored chunk resets
    back to dim — never a full ANSI reset — to keep the ` · ` separators,
    availability, and waiting segments dim around it.
    """
    if not colorize or not color:
        return text
    return f"{color}{text}{_ANSI_DIM}"


def _loop_recency_color(seconds_until_tick: float | None, cadence_seconds: int) -> str:
    """Map a loop's imminence to an ANSI color, RELATIVE to its own cadence.

    The color tracks the fraction of the loop's cadence still remaining until
    its next tick (``seconds_until_tick / cadence_seconds``): green when over
    half the cadence is left (just ticked / plenty of time), yellow as it
    approaches, red when it is about to tick or already overdue. Judging the
    fraction rather than absolute seconds keeps the signal relative — the same
    "120s until tick" reads green on an hourly loop and red on a 150s loop.

    ``None`` seconds (no acquire instant / never fired) and a non-positive
    cadence both fail safe to red (overdue / unknown).
    """
    if seconds_until_tick is None or cadence_seconds <= 0:
        return _ANSI_RED
    fraction = seconds_until_tick / cadence_seconds
    if fraction >= _RECENCY_GREEN_FRACTION:
        return _ANSI_GREEN
    if fraction >= _RECENCY_YELLOW_FRACTION:
        return _ANSI_YELLOW
    return _ANSI_RED


def _next_tick_minutes(name: str, acquired_at: datetime | None, cadence_of: CadenceResolver) -> str:
    """Return *name*'s relative next-tick as whole minutes (``11m`` / ``due``).

    Empty string when nothing useful is queryable (no acquire timestamp, no
    resolvable cadence) — the caller then renders a name-only chunk rather
    than invent a duration. Fails open on every cadence read.
    """
    if acquired_at is None:
        return ""
    try:
        cadence = cadence_of(name)
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to empty
        return ""
    return _relative_minutes(acquired_at + timedelta(seconds=cadence))


def _loop_chunk(name: str, acquired_at: datetime | None, cadence_of: CadenceResolver) -> str:
    """Render one ``<short-name> <next-tick>`` chunk for a live loop."""
    tick = _next_tick_minutes(name, acquired_at, cadence_of)
    suffix = f" {tick}" if tick else ""
    return f"{_short_loop_name(name)}{suffix}"


def _mini_loop_chunk(name: str, next_fire_at: datetime | None) -> str:
    """Render one ``<name> <next-tick>`` chunk for an enabled mini-loop.

    ``due`` when the loop has never fired (no marker → ``next_fire_at`` is
    ``None``) or is already overdue; otherwise the relative whole-minute
    countdown derived live from ``next_fire_at``.
    """
    tick = "due" if next_fire_at is None else _relative_minutes(next_fire_at)
    return f"{name} {tick}"


def _lease_recency_color(name: str, acquired_at: datetime | None, cadence_of: CadenceResolver) -> str:
    """Resolve the recency color for an infra lease from its own cadence."""
    try:
        cadence = cadence_of(name)
    except Exception:  # noqa: BLE001 — an unresolvable cadence degrades to the red recency color
        return _ANSI_RED
    seconds_until = _seconds_until(acquired_at + timedelta(seconds=cadence)) if acquired_at is not None else None
    return _loop_recency_color(seconds_until, cadence)


def _driver_suffix(name: str, drivers: dict[str, tuple[str, str]], *, colorize: bool) -> str:
    """Return the ``·<driver>`` / ``·DRIVERLESS`` suffix for a per-loop owner chunk.

    Only ``loop:<name>`` per-loop owner leases carry a driver chip in the shared
    loop line (``t3-master`` has its own anchor; infra leases never do — pinning
    edge-case 6, e.g. ``loop-reinstall`` renders no chip). An owned row (non-empty
    ``session_id``) with a registered driver renders ``·<driver>`` palette-neutral;
    an unowned row renders nothing.

    A blank stored driver only warrants the ``·DRIVERLESS`` alert when the loop is
    genuinely not ticking. A worker/cron tick runs anonymously (empty
    ``session_id``), so :meth:`LoopLeaseQuerySet.claim_ownership` never rewrites the
    owner lease and its stored ``driver`` fossilises blank while the loop ticks fine
    (#3366). ``·DRIVERLESS`` means "claimed but never ticks", so a loop the cadence
    ledger shows ticking (:func:`loop_is_actively_ticking`) suppresses the alert.
    """
    if not is_per_loop_owner_slot(name):
        return ""
    session_id, driver = drivers.get(name, ("", ""))
    if not session_id:
        return ""
    if driver:
        return f"·{driver}"
    if loop_is_actively_ticking(name):
        return ""
    return "·" + _colorize_chunk("DRIVERLESS", _ANSI_YELLOW, colorize=colorize)


def _lease_is_due_now(name: str, acquired_at: datetime | None, cadence_of: CadenceResolver) -> bool:
    """Whether a per-loop lease is due now / within the #3248 due-soon horizon.

    A never-acquired lease (no tick recorded yet) reads as due. Otherwise the
    loop is due when its own cadence puts the next fire within
    :data:`_DUE_SOON_SECONDS` — the same horizon the ``due:`` section applies to
    the mini-loop crons, so the two due views stay consistent. Fails open to
    ``True`` (surface the chunk) on any cadence-read error.
    """
    if acquired_at is None:
        return True
    try:
        cadence = cadence_of(name)
    except Exception:  # noqa: BLE001 — an unresolvable cadence keeps the chunk visible (never hide on error)
        return True
    return _seconds_until(acquired_at + timedelta(seconds=cadence)) <= _DUE_SOON_SECONDS


def _lease_chunk_surfaces(
    name: str, acquired_at: datetime | None, overridden: set[str] | None, cadence_of: CadenceResolver
) -> bool:
    """Whether a live lease still earns its own per-loop chunk (#3248 collapse).

    Infra / master leases (``loop-tick``, ``reinstall`` and friends — anything
    that is NOT a ``loop:<name>`` owner slot) always surface. A routine per-loop
    ``loop:<name>`` owner slot — claimed for EVERY enabled loop by the shared
    ``loop_runner`` — is collapsed into the preset/schedule handle unless it is
    manually overridden away from that handle or due to fire now, so the loop
    line stops listing every enabled loop.

    ``overridden is None`` is the fail-open marker (no collapse selector injected,
    or a resolver error): every chunk surfaces, i.e. today's behavior, so a broken
    override read can never hide a loop the operator expects to see.
    """
    if overridden is None:
        return True
    if not is_per_loop_owner_slot(name):
        return True
    if per_loop_loop_name(name) in overridden:
        return True
    return _lease_is_due_now(name, acquired_at, cadence_of)


def lease_chunks(
    leases: list[tuple[str, datetime | None]], context: LeaseRenderContext, *, colorize: bool = False
) -> list[str]:
    """Return one ``<short-name> <next-tick>`` chunk per live lease this session shows.

    The ``t3-master`` lease is excluded (a session-ownership token, not a work
    loop) and the transient per-loop tick mutex ``loop-tick:<name>`` too (while it
    is held the matching ``loop:<name>`` owner lease is held as well, so rendering
    it would show the ticking loop twice). The ``loop:<name>`` leases are
    per-session scoped via ``context.owned_per_loop``; the single ``loop_runner``
    session drives one for EVERY enabled loop, so ``context.overridden`` (when not
    ``None``) collapses the routine ones into the preset/schedule handle
    (:func:`_lease_chunk_surfaces`) — infra / master leases always survive. When
    *colorize* is set, each chunk is wrapped in its recency color.
    """
    cadence_of = context.cadence_of
    return [
        _colorize_chunk(
            _loop_chunk(name, acquired_at, cadence_of),
            _lease_recency_color(name, acquired_at, cadence_of),
            colorize=colorize,
        )
        + _driver_suffix(name, context.drivers, colorize=colorize)
        for name, acquired_at in leases
        if name != "t3-master"
        and not is_transient_tick_mutex(name)
        and per_loop_chunk_visible(name, context.owned_per_loop)
        and _lease_chunk_surfaces(name, acquired_at, context.overridden, cadence_of)
    ]


def mini_loop_chunks(schedules: list[tuple[str, datetime | None, int]], *, colorize: bool = False) -> list[str]:
    """Return a ``<name> <next-tick>`` chunk per DUE-SOON domain mini-loop (#3248).

    Companion to :func:`lease_chunks`: this renders the enabled domain crons from
    the cadence ledger that are due now (never fired / overdue) or within
    :data:`_DUE_SOON_SECONDS` — NOT the full loop list (presets/schedules are the
    handle). Each carries its own next-tick countdown and (when *colorize* is set)
    its cadence-relative recency color.
    """
    return [
        _colorize_chunk(
            _mini_loop_chunk(name, next_fire_at),
            _loop_recency_color(None if next_fire_at is None else _seconds_until(next_fire_at), cadence),
            colorize=colorize,
        )
        for name, next_fire_at, cadence in schedules
        if next_fire_at is None or _seconds_until(next_fire_at) <= _DUE_SOON_SECONDS
    ]
