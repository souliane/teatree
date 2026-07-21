from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from teatree.loop.loop_cadences import (
    drain_cadence_seconds,
    loop_owner_ttl_seconds,
    self_improve_cadence_seconds,
    slack_answer_cadence_seconds,
)
from teatree.loop.loop_scoping import current_session_owned_per_loop_slots
from teatree.loop.statusline_loop_chunks import (
    LeaseRenderContext,
    _colorize_chunk,
    lease_chunks,
    mini_loop_chunks,
    overdue_mini_loop_names,
)
from teatree.loop.statusline_palette import _ANSI_GREEN, _ANSI_RED, _ANSI_YELLOW

if TYPE_CHECKING:
    from teatree.core.managers import OwnershipStatus


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


def dashboard_head_anchor(*, colorize: bool = False) -> list[str]:
    """Return the single consolidated dashboard head line, or ``[]``.

    Folds the live-loops line (which already carries the merged ``mode:`` handle,
    #61), the configured-overlays summary, and the global-health chip onto
    ONE line joined by the loop line's own `` · `` separator — so overlays and
    health stop each wasting a whole row. Every source is individually
    fail-open, so a broken read drops only its own segment; the line is ``[]``
    only when all three are empty.
    """
    parts = [*live_loops_anchor(colorize=colorize), *overlays_anchor(), *health_chip(colorize=colorize)]
    if not parts:
        return []
    return [" · ".join(parts)]


def _live_loop_leases() -> list[tuple[str, datetime | None]]:
    """Return ``(loop_name, acquired_at)`` for every currently-live LoopLease.

    Isolated as a thin DB-read seam so :func:`live_loops_anchor` stays a
    pure formatter — tests stub this function rather than constructing
    LoopLease fixtures, and the renderer keeps a single try/except gate
    around it for fail-open semantics. ``acquired_at`` is ``None`` for a
    lease row that has never recorded an acquire (no tick yet); the
    renderer then drops the per-loop countdown for that loop.
    """
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    lease_model = apps.get_model("core", "LoopLease")
    rows = lease_model.objects.filter(lease_expires_at__gt=timezone.now()).only("name", "acquired_at").order_by("name")
    return [(row.name, row.acquired_at) for row in rows]


def _live_lease_drivers() -> dict[str, tuple[str, str]]:
    """Return ``{name: (session_id, driver)}`` for every live LoopLease row.

    A thin DB-read seam (parallel to :func:`_live_loop_leases`) so the driver chip
    stays a pure formatter concern — tests stub it rather than construct rows. Only
    the pid-anchored ownership layer (``loop:<name>``) reads its driver from here;
    a blank driver on an owned ``loop:<name>`` row is DRIVERLESS unless the loop is
    actually ticking (:func:`_driver_suffix`, #3366). Fails open to ``{}`` so a
    broken read only drops the chips, never blanks the loop line.
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
    global _mini_loop_schedules_reader  # noqa: PLW0603 — module-level reader rebound once via this single sanctioned setter
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


@dataclass(frozen=True, slots=True)
class PresetLineHandles:
    """The three ordered handles the loop line renders around the loop chunks (#3494, #61).

    The summary handles LEAD the line and the per-loop overrides trail the loops,
    so the single up-stack reader resolves the three sub-segments separately
    rather than pre-joining them:

    *   ``schedule`` — the active weekly schedule, spelled out:
        ``schedule: standard`` or ``schedule: none active`` (always shown).
    *   ``mode`` — the merged operating mode: ``mode: <name>`` (schedule/default)
        or ``mode: manual`` (manual override). Replaces the old ``preset:`` +
        ``availability:`` handles — the mode name conveys reachability.
    *   ``override`` — the ``forced ON: <names>`` / ``forced OFF: <names>``
        per-loop manual-override segment; ``""`` when none diverge.

    The renderer drops the empty ones.
    """

    schedule: str
    mode: str
    override: str


# The active preset/schedule/override handles (#3159, #3248, #3494) are resolved
# up-stack in :func:`teatree.loops.preset_status.preset_line_handles` (the resolver
# lives in ``teatree.loops``), injected here through the same seam as the mini-loop
# reader. Absent injection (a quiet machine, a unit test) the default reader returns
# empty handles and the preset segments are simply omitted — never a crash.
type PresetLineReader = Callable[[], PresetLineHandles]


def _empty_preset_handles() -> PresetLineHandles:
    return PresetLineHandles(schedule="", mode="", override="")


_preset_line_reader: PresetLineReader = _empty_preset_handles


def set_preset_line_reader(reader: PresetLineReader | None) -> None:
    """Install the up-stack preset-line handles reader (``None`` resets to empty)."""
    global _preset_line_reader  # noqa: PLW0603 — process-global injection seam, mirrors set_mini_loop_schedules_reader
    _preset_line_reader = reader or _empty_preset_handles


def _preset_line_handles() -> PresetLineHandles:
    """Return the active schedule / preset / override handles, or empty ones.

    Fails open to empty handles on any read error so a broken preset resolver
    never blanks the loop line.
    """
    try:
        return _preset_line_reader()
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to empty handles
        return _empty_preset_handles()


# The set of manually-overridden loop names (#3248) is resolved up-stack in
# :func:`teatree.loops.preset_status.overridden_loop_names` (the resolver lives
# in ``teatree.loops``), injected through the same seam as the preset segment.
# It gates the per-loop-lease collapse: a routine ``loop:<name>`` owner slot —
# claimed for EVERY enabled loop by the shared ``loop_runner`` — is folded into
# the preset/schedule handle unless it is manually overridden away from that
# handle (or due to fire now). Absent injection (a quiet machine, a unit test)
# the default reader signals ``None`` and NOTHING is collapsed — today's full
# per-loop render — so the collapse never activates without the preset handle
# that represents the folded loops also being present (both are injected
# together by the ``loops_tick`` per-loop command).
type OverriddenLoopsReader = Callable[[], set[str]]


_overridden_loops_reader: OverriddenLoopsReader | None = None


def set_overridden_loops_reader(reader: OverriddenLoopsReader | None) -> None:
    """Install the up-stack manually-overridden-loop-names reader (``None`` resets to uninjected).

    Mirrors :func:`set_preset_line_reader`; the ``loops_tick`` per-loop
    command installs it alongside the preset-line reader so the collapse and
    the ``ovr:`` handle are always rendered together, then resets it after the
    tick so the process-global seam never leaks.
    """
    global _overridden_loops_reader  # noqa: PLW0603 — process-global injection seam, mirrors set_preset_line_reader
    _overridden_loops_reader = reader


def _overridden_loops() -> set[str] | None:
    """Return the manually-overridden loop names, or ``None`` when no collapse applies.

    ``None`` is the fail-open sentinel: no reader injected (a quiet machine, a
    unit test) OR a resolver error. The caller reads ``None`` as "do not collapse
    — keep every per-loop chunk", i.e. today's behavior, so a broken override
    read can never hide a loop the operator expects to see. A resolved set
    (possibly empty) activates the #3248 per-loop-lease collapse.
    """
    reader = _overridden_loops_reader
    if reader is None:
        return None
    try:
        return reader()
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to no collapse
        return None


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
    from teatree.config import cadence_seconds  # noqa: PLC0415 — deferred: loaded at tick time, not import

    return cadence_seconds()


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


def _live_lease_chunks(*, colorize: bool = False, handle_present: bool = False) -> list[str]:
    """Return one ``<short-name> <next-tick>`` chunk per live LoopLease this session shows.

    The DB-read seam of the per-loop lease list: it reads the live leases, the
    driver map, this session's owned ``loop:<name>`` slots, and — only when
    *handle_present* (a governing preset/schedule segment is on the line to
    represent the folded loops) — the injected override set, then hands them plus
    the cadence resolver to :func:`teatree.loop.statusline_loop_chunks.lease_chunks`
    for the pure formatting + #3248 collapse. With no handle (or no injected
    selector) every lease surfaces — today's behavior — so a routine loop is never
    hidden without the handle that represents it. Fails open to ``[]`` on a
    lease-read error.
    """
    try:
        leases = _live_loop_leases()
    except Exception:  # noqa: BLE001 — rendering is best-effort; a lease-read failure degrades to no rows
        return []
    context = LeaseRenderContext(
        drivers=_live_lease_drivers(),
        owned_per_loop=current_session_owned_per_loop_slots(),
        overridden=_overridden_loops() if handle_present else None,
        cadence_of=_cadence_for_loop,
    )
    return lease_chunks(leases, context, colorize=colorize)


def live_loops_anchor(*, colorize: bool = False) -> list[str]:
    """Return the single dedicated loop line for the dashboard (#1400, #130).

    Single line, prepended at the top of the statusline so the user's
    "what governs now, what's overdue, and am I blocked?" question is answered
    with one glance. Spelled-out, labeled order (#3494, #61):

        ``schedule: none active · mode: manual · overdue: snapshot_warmer,
        triage_assessor · forced ON: triage_assessor · 4 waiting``

    Shape, in render order:

    *   ``schedule: <name>`` / ``schedule: none active`` — the active weekly
        schedule, always spelled out (:func:`teatree.loops.preset_status.schedule_chunk`).
    *   ``mode: <name>`` (schedule/default) / ``mode: manual`` (manual override) —
        the merged operating mode (#61), replacing the old ``preset:`` +
        ``availability:`` handles. The mode name conveys reachability, so an
        away-class mode (``unattended`` / ``offline``) needs no separate
        availability segment. A NON-``off`` mode is the summary handle the domain
        loops fold under.
    *   the loop section:

        -   with a mode governing, ``overdue: <name>, <name>`` — ONLY the
            genuinely-overdue / never-fired domain crons; the routine due-soon
            ones fold into the mode handle (:func:`_overdue_mini_loop_names`).
        -   with no mode handle, the full due-soon ``due: <name> <next-tick> …``
            countdown list (:func:`_mini_loop_chunks`) — every chunk's
            ``<next-tick>`` is the RELATIVE whole-minute countdown to THAT loop's
            own next fire, so a fast 60s cron and a slow 1h cron differ and the
            line counts down across renders.
    *   ``forced ON: <names>`` / ``forced OFF: <names>`` — the per-loop manual
        overrides that diverge from the mode/base verdict.
    *   ``N waiting`` — appended ONLY when N > 0 things are waiting on the user
        across the durable waiting-on-you lane (unresolved questions, PRs awaiting
        a merge authorization, pending review requests, manual items —
        :func:`teatree.core.waiting.gather_waiting`).
    *   the live infra :class:`~teatree.core.models.LoopLease` chunks (``tick``,
        ``self-improve``, ``slack-answer``, ``reinstall``) — kept unobtrusive at
        the TAIL (:func:`_live_lease_chunks`), never between the preset and the
        loops. The per-loop ``loop:<name>`` owner leases fold into the preset
        handle (#3248); the foreign-hijack case is NOT shown here — it is RED and
        routed to the action line by :func:`loop_owner_anchor`.

    Each chunk is colored by its imminence relative to its own cadence
    (:func:`_loop_recency_color`) when *colorize* is on. *colorize* defaults to
    ``False`` (the plain-text builder output); the render orchestrators
    (:func:`zones_for`, :func:`teatree.loop.tick.run_tick`) pass the
    ``NO_COLOR``-resolved value.

    Returns ``[]`` when nothing substantive is live — no schedule / preset /
    override handle, no loop section, and no infra lease — silencing the line on a
    quiet machine (the availability and waiting decorations never keep an
    otherwise-empty line alive). Fails open: any DB / import error degrades to
    ``[]`` (or, for an individual segment, drops just that segment) so a broken
    read can never blank the statusline.
    """
    # Resolve the schedule / preset / override handles first. The schedule and
    # preset lead the line as the summary handles the loops fold under, and an
    # ENGAGED PRESET (manual override or a schedule-driven one) drives the collapse
    # decision — a routine per-loop lease is only folded when a preset is present
    # to represent it, and the domain crons then show only their genuinely-overdue
    # exceptions. The override segment trails the loops it annotates.
    handles = _preset_line_handles()
    governing = bool(handles.mode)
    leases = _live_lease_chunks(colorize=colorize, handle_present=governing)

    # Under a governing mode the domain crons collapse to a single ``overdue:``
    # label listing only the genuinely-overdue / never-fired exceptions; with no
    # mode handle the full due-soon ``due:`` countdown list renders.
    if governing:
        overdue = _overdue_mini_loop_names()
        loop_section = "overdue: " + ", ".join(overdue) if overdue else ""
    else:
        due = _mini_loop_chunks(colorize=colorize)
        loop_section = "due: " + " ".join(due) if due else ""

    # The line is silenced only when nothing substantive is live — no schedule /
    # mode / override handle, no loop section, and no infra lease. The waiting
    # segment is a trailing decoration that never keeps an otherwise-empty line
    # alive (a quiet machine shows no loop line).
    substantive = handles.schedule or handles.mode or handles.override or loop_section or leases
    if not substantive:
        return []

    # Order (#3494, #61): schedule -> mode -> loops (overdue/due) -> overrides ->
    # waiting -> infra leases (kept unobtrusive at the tail, never between the mode
    # and the loops). The separate ``availability:`` segment is gone (folded into
    # ``mode:``).
    parts: list[str] = []
    if handles.schedule:
        parts.append(handles.schedule)
    if handles.mode:
        parts.append(handles.mode)
    if loop_section:
        parts.append(loop_section)
    if handles.override:
        parts.append(handles.override)
    waiting = _waiting_clause()
    if waiting:
        parts.append(waiting)
    parts.extend(leases)
    return [" · ".join(parts)]


def _waiting_clause() -> str:
    """Return ``N waiting`` when N > 0 things wait on the user, else ``""`` (PR-21).

    Fails open to ``""`` (no clause) on any read error so a broken
    :func:`teatree.core.waiting.gather_waiting` read never blanks the loop line.
    """
    try:
        count = _waiting_count()
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to empty
        return ""
    if count <= 0:
        return ""
    return f"{count} waiting"


def _waiting_count() -> int:
    """Count every entry waiting on the user across all kinds (PR-21).

    Thin DB-read seam so :func:`live_loops_anchor` stays a pure formatter —
    tests stub this rather than constructing the underlying fixtures. Scoped to
    every overlay (``""``) so the loop line shows the operator's whole backlog.
    """
    from teatree.core.waiting import gather_waiting  # noqa: PLC0415 — deferred: keep this module Django-free at import

    return len(gather_waiting(""))


def _mini_loop_chunks(*, colorize: bool = False) -> list[str]:
    """Return a ``<name> <next-tick>`` chunk per DUE-SOON domain mini-loop (#3248).

    The DB-read seam of the ``due:`` section (the no-preset path): reads the
    cadence ledger (:func:`_mini_loop_schedules`) then hands the schedules to
    :func:`teatree.loop.statusline_loop_chunks.mini_loop_chunks` for the due-soon
    filter + formatting. Fails open to ``[]`` on any DB / config read error so a
    broken ledger never blanks the line.
    """
    try:
        schedules = _mini_loop_schedules()
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to no chunks
        return []
    return mini_loop_chunks(schedules, colorize=colorize)


def _overdue_mini_loop_names() -> list[str]:
    """Return the names of genuinely-overdue / never-fired domain mini-loops (#3494).

    The DB-read seam of the ``overdue:`` section (the engaged-preset path): reads
    the cadence ledger (:func:`_mini_loop_schedules`) then hands the schedules to
    :func:`teatree.loop.statusline_loop_chunks.overdue_mini_loop_names` — the
    collapsed exceptions the preset handle does NOT already represent. Fails open
    to ``[]`` on any DB / config read error so a broken ledger never blanks the
    line.
    """
    try:
        schedules = _mini_loop_schedules()
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to no names
        return []
    return overdue_mini_loop_names(schedules)


def mini_loops_anchor() -> list[str]:
    """Return the plain (uncolored) mini-loop chunk list.

    Thin :func:`_mini_loop_chunks` wrapper kept for the public surface and
    direct callers that want the bare ``<name> <next-tick>`` strings without
    recency color.
    """
    return _mini_loop_chunks(colorize=False)
