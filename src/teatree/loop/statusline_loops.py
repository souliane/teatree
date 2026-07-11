from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from teatree.loop.loop_cadences import (
    drain_cadence_seconds,
    loop_owner_ttl_seconds,
    self_improve_cadence_seconds,
    slack_answer_cadence_seconds,
)
from teatree.loop.loop_scoping import (
    current_session_owned_per_loop_slots,
    is_per_loop_owner_slot,
    is_transient_tick_mutex,
    per_loop_chunk_visible,
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

if TYPE_CHECKING:
    from teatree.core.availability import Resolution
    from teatree.core.managers import OwnershipStatus


def availability_segment(resolution: "Resolution") -> str:
    """Return the loop line's availability segment (#58, #1678).

    Renders ``availability: <present|away> (<source>)`` so the user reads the
    currently-resolved availability and which layer decided it (override /
    schedule / default) at a glance, as one ``·``-separated segment of the
    dedicated loop line.

    The ``availability:`` label is deliberately distinct from the config
    ``Mode`` enum (auto/interactive) and other ``mode=`` usages, which the bare
    ``mode=away`` form collided with. An unrecognised mode renders nothing.
    """
    from teatree.core.availability import _VALID_MODES  # noqa: PLC0415

    if resolution.mode not in _VALID_MODES:
        return ""
    return f"availability: {resolution.mode} ({resolution.source})"


def _configured_overlay_names() -> list[str]:
    """Return the sorted names of every configured overlay.

    Thin discovery seam so :func:`overlays_anchor` stays a pure formatter —
    tests stub this rather than registering real overlays. Production reads
    the unified entry-point + DB overlays-registry discovery in
    :meth:`teatree.core.overlay_loader.OverlayConfigResolver.all_names`.
    """
    from teatree.core.overlay_loader import OverlayConfigResolver  # noqa: PLC0415 — deferred read

    return sorted(OverlayConfigResolver.all_names())


def overlays_anchor() -> list[str]:
    """Return the single configured-overlays summary line, or ``[]``.

    Surfaces the user's multi-overlay context (``overlays: a · b · c``)
    directly, rather than leaving overlays to appear only implicitly when a
    ticket or PR happens to carry an ``[ov]`` prefix. Returns ``[]`` when no
    overlay is configured. Fails open: any discovery error degrades to ``[]``
    so a broken config can never blank the statusline.
    """
    try:
        names = _configured_overlay_names()
    except Exception:  # noqa: BLE001 — fail-open: a broken config can never blank the statusline
        return []
    if not names:
        return []
    return [f"overlays: {' · '.join(names)}"]


def health_chip(*, colorize: bool = False) -> list[str]:
    """Return the single global-health chip line, or ``[]`` (PR-17).

    Reads the persisted operational-health verdict (read-only —
    :func:`teatree.core.factory.operational_health.read_health`, never a reconcile at
    render time) and renders a colored status dot plus the open-issue count:
    ``health: ●`` when green and clean, ``health: ● 3`` when three issues are
    open. The dot is green/yellow/red per the verdict; when *colorize* is set it
    resets to the loop line's dim baseline (not a full reset) so the ``health:``
    label and count stay dim around it. Fails open to ``[]`` so a broken read
    never blanks the statusline.
    """
    try:
        from teatree.core.factory.operational_health import HealthStatus, read_health  # noqa: PLC0415 — deferred read

        report = read_health()
    except Exception:  # noqa: BLE001 — fail-open: a broken health read never blanks the statusline
        return []
    color = {
        HealthStatus.GREEN: _ANSI_GREEN,
        HealthStatus.YELLOW: _ANSI_YELLOW,
        HealthStatus.RED: _ANSI_RED,
    }.get(report.status, _ANSI_GREEN)
    dot = _colorize_chunk("●", color, colorize=colorize)
    count = f" {report.open_count}" if report.open_count else ""
    return [f"health: {dot}{count}"]


def _live_loop_leases() -> list[tuple[str, datetime | None]]:
    """Return ``(loop_name, acquired_at)`` for every currently-live LoopLease.

    Isolated as a thin DB-read seam so :func:`live_loops_anchor` stays a
    pure formatter — tests stub this function rather than constructing
    LoopLease fixtures, and the renderer keeps a single try/except gate
    around it for fail-open semantics. ``acquired_at`` is ``None`` for a
    lease row that has never recorded an acquire (no tick yet); the
    renderer then drops the per-loop countdown for that loop.
    """
    from django.apps import apps  # noqa: PLC0415
    from django.utils import timezone  # noqa: PLC0415

    lease_model = apps.get_model("core", "LoopLease")
    rows = lease_model.objects.filter(lease_expires_at__gt=timezone.now()).only("name", "acquired_at").order_by("name")
    return [(row.name, row.acquired_at) for row in rows]


def _live_lease_drivers() -> dict[str, tuple[str, str]]:
    """Return ``{name: (session_id, driver)}`` for every live LoopLease row.

    A thin DB-read seam (parallel to :func:`_live_loop_leases`) so the driver chip
    stays a pure formatter concern — tests stub it rather than construct rows. Only
    the pid-anchored ownership layer (``loop:<name>``) reads its driver from here;
    a blank driver on an owned ``loop:<name>`` row is DRIVERLESS. Fails open to
    ``{}`` so a broken read only drops the chips, never blanks the loop line.
    """
    from django.apps import apps  # noqa: PLC0415 — deferred: keep this module Django-free at import
    from django.utils import timezone  # noqa: PLC0415 — deferred: keep this module Django-free at import

    try:
        lease_model = apps.get_model("core", "LoopLease")
        rows = lease_model.objects.filter(lease_expires_at__gt=timezone.now()).only("name", "session_id", "driver")
        return {row.name: (row.session_id, row.driver) for row in rows}
    except Exception:  # noqa: BLE001 — fail-open: a broken driver read never blanks the loop line
        return {}


# The per-mini-loop next-fire reader lives up-stack in
# :func:`teatree.loops.schedule.mini_loop_schedules` because resolving it
# needs the mini-loop registry and ``[loops]`` config, both of which live in
# :mod:`teatree.loops` — and the tach module graph forbids
# :mod:`teatree.loop` from importing :mod:`teatree.loops` (the dependency
# points the other way). Mirroring the ``jobs_builder`` seam in
# :func:`teatree.loop.tick.run_tick`, the live entry point (the ``loops_tick``
# per-loop command) injects the real reader via
# :func:`set_mini_loop_schedules_reader`; absent injection (a quiet machine,
# a unit test) the default reader returns ``[]`` and the mini-loop chunks are
# simply omitted — never an import-direction violation, never a crash.
type MiniLoopSchedule = tuple[str, datetime | None, int]
type MiniLoopSchedulesReader = Callable[[], list[MiniLoopSchedule]]


def _empty_mini_loop_schedules() -> list[MiniLoopSchedule]:
    return []


_mini_loop_schedules_reader: MiniLoopSchedulesReader = _empty_mini_loop_schedules


def set_mini_loop_schedules_reader(reader: MiniLoopSchedulesReader | None) -> None:
    """Install the up-stack mini-loop next-fire reader (``None`` resets to empty).

    Called by each ``loops_tick`` per-loop command — the only place
    allowed to bridge :mod:`teatree.loops` into the statusline without
    violating the tach module graph.
    """
    global _mini_loop_schedules_reader  # noqa: PLW0603
    _mini_loop_schedules_reader = reader or _empty_mini_loop_schedules


def _mini_loop_schedules() -> list[MiniLoopSchedule]:
    """Return ``(loop_name, next_fire_at, cadence_seconds)`` per enabled mini-loop.

    Delegates to the injected reader (:func:`set_mini_loop_schedules_reader`).
    Each domain mini-loop (``dispatch``, ``tickets``, ``review``, ``ship``,
    ``inbox``, ``resource_pressure``, …) is a cron with its own cadence: its
    ``next_fire_at`` is the cadence-ledger ``last_fired_at`` plus the loop's
    resolved cadence, or ``None`` when the loop has never fired (the renderer
    surfaces that as ``due``). ``cadence_seconds`` is that resolved cadence —
    the denominator the renderer colors each chunk against.
    """
    return _mini_loop_schedules_reader()


# The active-preset statusline segment (#3159) is resolved up-stack in
# :func:`teatree.loops.preset_status.statusline_chunk` (the resolver lives in
# ``teatree.loops``), injected here through the same seam as the mini-loop
# reader. Absent injection (a quiet machine, a unit test) the default reader
# returns ``""`` and the preset segment is simply omitted — never a crash.
type PresetSegmentReader = Callable[[], str]


def _empty_preset_segment() -> str:
    return ""


_preset_segment_reader: PresetSegmentReader = _empty_preset_segment


def set_preset_segment_reader(reader: PresetSegmentReader | None) -> None:
    """Install the up-stack active-preset segment reader (``None`` resets to empty)."""
    global _preset_segment_reader  # noqa: PLW0603 — process-global injection seam, mirrors set_mini_loop_schedules_reader
    _preset_segment_reader = reader or _empty_preset_segment


def _preset_segment() -> str:
    """Return the active-preset statusline segment (``preset heads-down →19:00``), or ``""``.

    Fails open to ``""`` on any read error so a broken preset resolver never
    blanks the loop line.
    """
    try:
        return _preset_segment_reader()
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to empty
        return ""


# Per-loop cadence resolution (#1400). Each named loop ticks on its own
# schedule, so the next-tick countdown is ``acquired_at + cadence`` with the
# loop's own cadence — not a single shared value. Unknown / future loops fall
# back to the ``loop-tick`` cadence so a newly-added loop surfaces without a
# code change here. The per-loop cadence readers live next to each loop's
# owning module; this is the single place that maps a loop name to its reader.
def _cadence_for_loop(name: str) -> int:
    """Return the cadence in seconds for the named loop (``loop-tick`` fallback)."""
    if name == "loop-slack-answer":
        return slack_answer_cadence_seconds()
    if name == "loop-self-improve":
        return self_improve_cadence_seconds()
    if name == "t3-master":
        return loop_owner_ttl_seconds()
    if name == "loop-drain-queue":
        return drain_cadence_seconds()
    return _cadence_seconds()


def _cadence_seconds() -> int:
    """Return the resolved ``loop-tick`` cadence in seconds.

    Isolated as a seam so tests can stub it without spinning up the
    full config layer; production reads :func:`teatree.config.cadence_seconds`.
    Doubles as the fallback cadence for loops with no dedicated reader.
    """
    from teatree.config import cadence_seconds  # noqa: PLC0415

    return cadence_seconds()


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


def loop_owner_anchor(status: "OwnershipStatus", this_session: str) -> tuple[str, str]:
    """Return ``(zone, line)`` for the foreign-hijack RED line (#1073, #1156).

    #1156 narrowed this to only the foreign-hijack RED case. The dim
    ``t3-master=THIS session ✓`` and ``t3-master=unclaimed`` lines were
    replaced by :func:`live_loops_anchor`, which renders one line per
    live :class:`teatree.core.models.LoopLease` row.

    A *different* live session owns it → ``("action_needed",
    "t3-master=session <short8> (NOT this session)")`` — RED, because a
    foreign owner is exactly the #1073 hijack the user must see.

    THIS session owns it but registered NO tick driver → ``("action_needed",
    "t3-master=this session · DRIVERLESS")`` — RED, because a driverless master
    never ticks (PR-26). This session owns it WITH a driver, or no live owner →
    ``("anchors", "")``. Callers suppress empty lines.

    ``short8`` is the first 8 chars of the owner session id.
    """
    if not status.is_live:
        return "anchors", ""
    if this_session and status.owner_session == this_session:
        if not status.driver:
            return "action_needed", "t3-master=this session · DRIVERLESS"
        return "anchors", ""
    short8 = status.owner_session[:8]
    return "action_needed", f"t3-master=session {short8} (NOT this session)"


def _live_lease_chunks(*, colorize: bool = False) -> list[str]:
    """Return one ``<short-name> <next-tick>`` chunk per live LoopLease this session shows.

    The ``t3-master`` lease is excluded: it is a session-ownership token,
    not a work loop, and its countdown is meaningless in the shared zones
    file (the per-session owner badge in ``statusline.sh`` replaces that
    signal). The transient per-loop tick mutex ``loop-tick:<name>`` (#2650) is
    excluded too: it is a concurrency lock held only for the beat, and while it
    is held the matching ``loop:<name>`` owner lease is held as well — so
    rendering it would show the currently-ticking loop twice, once as
    ``tick:<name>`` and once as ``loop:<name>``. The dedicated-loop
    ``loop:<name>`` leases (#1834) are **per-session scoped** via
    :mod:`teatree.loop.loop_scoping` — only the loops THIS session owns survive
    (fail-open, byte-identical under the single-owner default). When *colorize*
    is set, each chunk is wrapped in its recency color
    (:func:`_loop_recency_color`); fails open to ``[]``.
    """
    try:
        leases = _live_loop_leases()
    except Exception:  # noqa: BLE001 — rendering is best-effort; a lease-read failure degrades to no rows
        return []
    owned_per_loop = current_session_owned_per_loop_slots()
    drivers = _live_lease_drivers()
    return [
        _colorize_chunk(
            _loop_chunk(name, acquired_at),
            _lease_recency_color(name, acquired_at),
            colorize=colorize,
        )
        + _driver_suffix(name, drivers, colorize=colorize)
        for name, acquired_at in leases
        if name != "t3-master" and not is_transient_tick_mutex(name) and per_loop_chunk_visible(name, owned_per_loop)
    ]


def _driver_suffix(name: str, drivers: dict[str, tuple[str, str]], *, colorize: bool) -> str:
    """Return the ``·<driver>`` / ``·DRIVERLESS`` suffix for a per-loop owner chunk.

    Only ``loop:<name>`` per-loop owner leases carry a driver chip in the shared
    loop line (``t3-master`` has its own anchor; infra leases never do — pinning
    edge-case 6, e.g. ``loop-reinstall`` renders no chip). An owned row (non-empty
    ``session_id``) with a registered driver renders ``·<driver>`` palette-neutral;
    an owned row with a blank driver renders ``·DRIVERLESS`` in the alert palette;
    an unowned row renders nothing.
    """
    if not is_per_loop_owner_slot(name):
        return ""
    session_id, driver = drivers.get(name, ("", ""))
    if not session_id:
        return ""
    if driver:
        return f"·{driver}"
    return "·" + _colorize_chunk("DRIVERLESS", _ANSI_YELLOW, colorize=colorize)


def _lease_recency_color(name: str, acquired_at: datetime | None) -> str:
    """Resolve the recency color for an infra lease from its own cadence."""
    try:
        cadence = _cadence_for_loop(name)
    except Exception:  # noqa: BLE001 — an unresolvable cadence degrades to the red recency color
        return _ANSI_RED
    seconds_until = _seconds_until(acquired_at + timedelta(seconds=cadence)) if acquired_at is not None else None
    return _loop_recency_color(seconds_until, cadence)


def _seconds_until(next_fire_at: datetime) -> float:
    """Return seconds from now until *next_fire_at* (negative once overdue)."""
    from django.utils import timezone  # noqa: PLC0415

    return (next_fire_at - timezone.now()).total_seconds()


def live_loops_anchor(*, colorize: bool = False) -> list[str]:
    """Return the single dedicated loop line for the dashboard (#1400, #130).

    Single line, prepended at the top of the statusline so the user's
    "which loops are running, when does each tick next, and am I blocked?"
    question is answered with one glance:

        ``tick 11m · dispatch 2m · tickets 4m · news 18m · waiting: 2 questions``

    Shape:

    *   the line leads with the live loops' own chunks. The line is only
        ever rendered when at least one loop or cron is active; when none
        is, the function returns ``[]`` and the line is silenced entirely
        (no ``idle`` line is shown). The ``tick <next-tick>`` chunk already
        carries the loop's liveness, so no separate ``loop running`` state
        word precedes it. The foreign-hijack case is NOT shown here — it is
        RED and routed to the action line by :func:`loop_owner_anchor`.
    *   one ``<short-name> <next-tick>`` chunk per live infra
        :class:`~teatree.core.models.LoopLease` (``tick``,
        ``self-improve``, ``slack-answer``) — see :func:`_live_lease_chunks`.
    *   one ``<name> <next-tick>`` chunk per ENABLED domain mini-loop /
        cron (``dispatch``, ``tickets``, ``review``, ``ship``, ``inbox``,
        ``resource_pressure``, …) — see :func:`_mini_loop_chunks`. Every
        chunk's ``<next-tick>`` is the RELATIVE whole-minute countdown to
        THAT loop's own next fire (``2m``), derived live from its own
        cadence and last-fired instant — not one shared constant — so a
        fast 60s cron and a slow 1h cron show different countdowns and the
        whole line counts down across renders. ``due`` replaces the
        duration when a loop is overdue or has never fired.
    *   ``availability: <present|away> (<source>)`` — the currently-resolved
        availability, read live at render time (:func:`_availability_segment`)
        so the user always sees the present/away value and which layer decided
        it, never a cached one.
    *   ``waiting=N`` — appended ONLY when N > 0 things are waiting on the
        user across the durable waiting-on-you lane (unresolved questions,
        PRs awaiting a merge authorization, pending review requests, manual
        items — :func:`teatree.core.waiting.gather_waiting`), so the dashboard
        surfaces "you owe N things" without the user hunting for them.

    Each per-loop chunk is colored by its imminence relative to its own
    cadence (:func:`_loop_recency_color`) when *colorize* is on — green when
    it just ticked / has plenty of time, yellow as it approaches, red when it
    is about to tick or overdue — so the user reads at a glance which loops
    are fresh and which are due. *colorize* defaults to ``False`` (the
    plain-text builder output); the render orchestrators (:func:`zones_for`,
    :func:`teatree.loop.tick.run_tick`) pass the ``NO_COLOR``-resolved value.
    The availability and waiting segments stay the line's baseline dim.

    Returns ``[]`` when neither an infra lease nor an enabled mini-loop is
    active — silences the line entirely on a quiet machine. Fails open: any
    DB / import error degrades to ``[]`` (or, for an individual segment,
    drops just that segment) so a broken read can never blank the statusline.
    """
    chunks = [*_live_lease_chunks(colorize=colorize), *_mini_loop_chunks(colorize=colorize)]
    if not chunks:
        return []

    parts = [*chunks]
    preset = _preset_segment()
    if preset:
        parts.append(preset)
    availability = _availability_segment()
    if availability:
        parts.append(availability)
    waiting = _waiting_clause()
    if waiting:
        parts.append(waiting)
    return [" · ".join(parts)]


def _availability_segment() -> str:
    """Return the live availability segment for the loop line, or ``""``.

    Reads :func:`teatree.core.availability.resolve_mode` at render time so the
    segment reflects the currently-resolved availability, never a cached value.
    Fails open to ``""`` (no segment) on any read error so a broken
    availability config never blanks the loop line.
    """
    try:
        from teatree.core.availability import resolve_mode  # noqa: PLC0415

        return availability_segment(resolve_mode())
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to empty
        return ""


def _waiting_clause() -> str:
    """Return ``waiting=N`` when N > 0 things wait on the user, else ``""`` (PR-21).

    Fails open to ``""`` (no clause) on any read error so a broken
    :func:`teatree.core.waiting.gather_waiting` read never blanks the loop line.
    """
    try:
        count = _waiting_count()
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to empty
        return ""
    if count <= 0:
        return ""
    return f"waiting={count}"


def _waiting_count() -> int:
    """Count every entry waiting on the user across all kinds (PR-21).

    Thin DB-read seam so :func:`live_loops_anchor` stays a pure formatter —
    tests stub this rather than constructing the underlying fixtures. Scoped to
    every overlay (``""``) so the loop line shows the operator's whole backlog.
    """
    from teatree.core.waiting import gather_waiting  # noqa: PLC0415 — deferred: keep this module Django-free at import

    return len(gather_waiting(""))


def _short_loop_name(name: str) -> str:
    """Strip the ``loop-`` prefix so ``loop-my-prs`` reads as ``my-prs``."""
    return name.removeprefix("loop-")


def _loop_chunk(name: str, acquired_at: datetime | None) -> str:
    """Render one ``<short-name> <next-tick>`` chunk for a live loop."""
    tick = _next_tick_minutes(name, acquired_at)
    suffix = f" {tick}" if tick else ""
    return f"{_short_loop_name(name)}{suffix}"


def _next_tick_minutes(name: str, acquired_at: datetime | None) -> str:
    """Return *name*'s relative next-tick as whole minutes (``11m`` / ``due``).

    Empty string when nothing useful is queryable (no acquire timestamp, no
    resolvable cadence) — the caller then renders a name-only chunk rather
    than invent a duration. Fails open on every config read.
    """
    if acquired_at is None:
        return ""
    try:
        cadence = _cadence_for_loop(name)
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to empty
        return ""
    return _relative_minutes(acquired_at + timedelta(seconds=cadence))


def _relative_minutes(next_fire_at: datetime) -> str:
    """Return the relative whole-minute countdown to *next_fire_at* (``11m`` / ``due``).

    ``due`` once the instant is in the past — the loop fires on the
    orchestrator's next tick. Always derived from the live clock at render
    time so the value counts down across successive renders rather than
    freezing on a cached string.
    """
    from django.utils import timezone  # noqa: PLC0415

    delta_seconds = int((next_fire_at - timezone.now()).total_seconds())
    if delta_seconds <= 0:
        return "due"
    minutes = max(1, round(delta_seconds / _SECONDS_PER_MINUTE))
    return f"{minutes}m"


def _mini_loop_chunk(name: str, next_fire_at: datetime | None) -> str:
    """Render one ``<name> <next-tick>`` chunk for an enabled mini-loop.

    ``due`` when the loop has never fired (no marker → ``next_fire_at`` is
    ``None``) or is already overdue; otherwise the relative whole-minute
    countdown derived live from ``next_fire_at``.
    """
    tick = "due" if next_fire_at is None else _relative_minutes(next_fire_at)
    return f"{name} {tick}"


def _mini_loop_chunks(*, colorize: bool = False) -> list[str]:
    """Return one ``<name> <next-tick>`` chunk per enabled domain mini-loop.

    Companion to :func:`_live_lease_chunks`: where that renders the infra
    leases (``loop-tick`` and friends), this renders every enabled domain
    cron from :func:`teatree.loops.registry.iter_loops` — ``dispatch``,
    ``tickets``, ``review``, ``ship``, ``inbox``, ``resource_pressure``, … —
    each with its own next-tick countdown derived from the cadence ledger
    (:func:`_mini_loop_schedules`), never a shared constant, and (when
    *colorize* is set) wrapped in its cadence-relative recency color. The two
    chunk lists compose into the single dedicated loop line in
    :func:`live_loops_anchor`.

    Returns ``[]`` when no mini-loop is enabled, or fails open to ``[]`` on
    any DB / config read error so a broken ledger never blanks the line.
    """
    try:
        schedules = _mini_loop_schedules()
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to no chunks
        return []
    return [
        _colorize_chunk(
            _mini_loop_chunk(name, next_fire_at),
            _loop_recency_color(None if next_fire_at is None else _seconds_until(next_fire_at), cadence),
            colorize=colorize,
        )
        for name, next_fire_at, cadence in schedules
    ]


def mini_loops_anchor() -> list[str]:
    """Return the plain (uncolored) mini-loop chunk list.

    Thin :func:`_mini_loop_chunks` wrapper kept for the public surface and
    direct callers that want the bare ``<name> <next-tick>`` strings without
    recency color.
    """
    return _mini_loop_chunks(colorize=False)
