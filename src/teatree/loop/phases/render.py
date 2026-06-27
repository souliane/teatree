"""``render_phase`` — finalize one tick into the statusline + sidecars.

The closing stage of a tick: project the dispatched actions into the
statusline zones, refresh the ``tick-meta.json`` freshness header and the
``open-prs.json`` cache, fold in the live-loop / open-PR / loop-owner
anchors, and write the rendered statusline. The idle (no-jobs) tick takes
the same closing stage with an empty zone set so even a quiet tick keeps
the running-loops and open-PR anchors live.

Splitting this out of ``run_tick`` leaves the loop's per-tick entry point a
thin pipeline of named phases — ``sweep`` then ``scan`` then ``act`` then
``render`` — with no inline body, so the fat monolithic tick no longer
exists as one unit.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.loop.domain_jobs import _identity_groups_for_overlay
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases.orchestrate import orchestrate_phase
from teatree.loop.rendering import zones_for
from teatree.loop.statusline import StatuslineZones, render
from teatree.loop.tick_freshness import _write_tick_meta

if TYPE_CHECKING:
    from teatree.loop.scanners.base import ScanSignal
    from teatree.loop.tick import TickReport, TickRequest

logger = logging.getLogger(__name__)


def render_phase(
    report: "TickReport",
    request: "TickRequest",
    *,
    jobs: list[_ScannerJob],
    statusline_path: Path | None = None,
    colorize: bool | None = None,
) -> None:
    """Render the tick's statusline + sidecars onto ``report.statusline_path``.

    An active tick (``jobs`` non-empty) projects the dispatched actions into
    statusline zones, refreshes ``tick-meta.json``, plans the admit budget,
    and surfaces any scanner errors. An idle tick (empty ``jobs``) renders an
    empty zone set carrying only the live-loop / open-PR / loop-owner anchors
    so a quiet tick still keeps the dashboard live. Both paths write the
    open-PR cache, fold in the open-PR + loop-owner anchors, and render.
    """
    if jobs:
        zones = zones_for(report.actions, colorize=colorize, identity_aliases=_identity_aliases_for_request(request))
        _write_tick_meta(report.started_at, target=statusline_path)
        _orchestrate(request, statusline_path=statusline_path)
    else:
        zones = StatuslineZones()
        _populate_live_loops_in_anchors(zones, colorize=colorize)
    _write_open_prs_cache(report.signals, target=statusline_path)
    _populate_open_prs_in_anchors(zones, target=statusline_path, colorize=colorize)
    if jobs and report.errors:
        zones.action_needed.append(f"scanner errors: {', '.join(report.errors)}")
    _populate_loop_owner_anchor(zones)
    report.statusline_path = render(zones, target=statusline_path, colorize=colorize)
    if not jobs:
        _write_tick_meta(report.started_at, target=statusline_path)


def _orchestrate(request: "TickRequest", *, statusline_path: Path | None) -> None:
    """Plan the admit BUDGET — the read-only fan-out ceiling.

    The reconciled fan-out keeps exactly ONE claim point: the live
    ``claim_next`` CAS. ``orchestrate_phase`` is a read-only PLANNER here — it
    never claims in the tick. Instead it computes the clamped fan-out cap and
    persists it as a BUDGET ceiling to the tick-meta sidecar for the live
    claimer to read.

    The DB-home ``orchestrate_claim_enabled`` toggle (default OFF; ``t3
    <overlay> config_setting set orchestrate_claim_enabled true``) gates the
    planner:

    *   **OFF (default)** — no budget key is written (any prior one is cleared),
        so the claimer reads UNCLAMPED. Byte-identical to before the arm.
    *   **ON** — at a clamping speed (``full``/``boost``/``slow``) the computed
        cap is persisted as the budget; at ``medium`` no budget key is written
        (absence = unclamped = today's throughput). The phase never claims, so
        there is no orphan window.

    Fully fail-open — any config read, planner, or sidecar error degrades to a
    no-op (and the toggle resolves to OFF on a settings-read error), like every
    other tick phase. A failed budget write leaves the sidecar without the key,
    which the reader treats as unclamped: fail-safe by construction.
    """
    try:
        from teatree.config import Speed  # noqa: PLC0415
        from teatree.loop.admit_budget import clear_admit_budget, write_admit_budget  # noqa: PLC0415
        from teatree.loop.statusline import default_path  # noqa: PLC0415

        target = statusline_path or default_path()
        if not _orchestrate_claim_enabled():
            clear_admit_budget(statusline_path=target)
            return
        manifest = orchestrate_phase(backends=request.backends, claim=False)
        if manifest.speed is Speed.MEDIUM:
            clear_admit_budget(statusline_path=target)
            return
        write_admit_budget(manifest.cap, statusline_path=target)
    except Exception:
        logger.exception("orchestrate_phase budget planning failed — tick continues")


def _orchestrate_claim_enabled() -> bool:
    """Resolve the ``orchestrate_claim_enabled`` toggle; fail OFF.

    Reads the effective setting (env -> DB -> per-overlay -> global -> default).
    Any settings-read error degrades to ``False`` so the dormant ``claim=False``
    path is the fail-safe — the arm can never fire on a broken config read.
    """
    try:
        return get_effective_settings().orchestrate_claim_enabled
    except Exception:  # noqa: BLE001
        return False


def _identity_aliases_for_request(request: "TickRequest") -> tuple[tuple[str, ...], ...]:
    """Union the identity-alias groups across every overlay in *request*.

    The renderer suppresses a reassignment between two handles of the same
    human and collapses each handle to its group's canonical name. Fails
    open: any config-read error degrades to no suppression.

    Routes through :func:`_identity_groups_for_overlay` (not the raw resolver)
    so the self-group fallback applies on the render path too: when no explicit
    ``identity_aliases`` is configured, the operator's ``backend.identities``
    form one implicit self-group. Without it the render-side group was empty
    and intra-self reassigns leaked as ``reassigned`` churn.
    """
    groups: list[tuple[str, ...]] = []
    try:
        for backend in request.backends or []:
            groups.extend(_identity_groups_for_overlay(backend))
    except Exception:  # noqa: BLE001
        return ()
    return tuple(groups)


def _populate_live_loops_in_anchors(zones: StatuslineZones, *, colorize: bool | None = None) -> None:
    """Append one anchor line per live LoopLease row.

    Used by the idle (empty-jobs) render so even an idle tick still surfaces
    the running loops. The active path goes through
    :func:`teatree.loop.rendering._populate_live_loops_anchor` via
    :func:`teatree.loop.rendering.zones_for` and must not double-populate.
    *colorize* threads the per-loop recency coloring through.

    Fails open: any import/query error degrades to a no-op.
    """
    try:
        from teatree.loop.statusline import colorize_enabled, live_loops_anchor  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    try:
        zones.anchors.extend(live_loops_anchor(colorize=colorize_enabled(colorize=colorize)))
    except Exception:  # noqa: BLE001
        return


def _write_open_prs_cache(signals: "list[ScanSignal]", *, target: Path | None) -> None:
    """Snapshot the tick's open PRs to the ``open-prs.json`` sidecar.

    Projects the ``my_pr.*`` signals the scanners already produced — no extra
    code-host call — so the statusline's open-PR anchor reads a fresh cache
    without ever hitting the API itself. Writing on every tick (even with an
    empty signal list) keeps the cache from going stale when the last PR is
    merged. Fails open: any import/write error degrades to a no-op so a broken
    snapshot can never abort the tick.
    """
    try:
        from teatree.loop.open_prs import open_prs_from_signals, write_open_prs_cache  # noqa: PLC0415
        from teatree.loop.statusline import default_path  # noqa: PLC0415

        write_open_prs_cache(open_prs_from_signals(signals), statusline_path=target or default_path())
    except Exception:  # noqa: BLE001
        return


def _populate_open_prs_in_anchors(zones: StatuslineZones, *, target: Path | None, colorize: bool | None = None) -> None:
    """Append the open-PR summary anchor.

    Reads the snapshot :func:`_write_open_prs_cache` just wrote and folds the
    count/list rows into the anchor zone. Must run AFTER the cache write so
    it reflects this tick, not the previous one. Fails open: any import/read
    error degrades to a no-op so a broken cache can never blank the statusline.
    """
    try:
        from teatree.loop.open_prs import open_prs_anchor  # noqa: PLC0415
        from teatree.loop.statusline import colorize_enabled, default_path  # noqa: PLC0415

        zones.anchors.extend(
            open_prs_anchor(target=target or default_path(), colorize=colorize_enabled(colorize=colorize))
        )
    except Exception:  # noqa: BLE001
        return


def _populate_loop_owner_anchor(zones: StatuslineZones) -> None:
    """Append the foreign-hijack loop-owner RED line.

    The live-loops anchor (the single dedicated loop line folding all live
    LoopLease rows) is populated separately by
    :func:`teatree.loop.rendering._populate_live_loops_anchor`. This function
    is responsible only for the foreign-hijack RED line surfaced when a
    different live session holds ``loop-owner``.

    Fails open: any import/query error degrades to a no-op so a broken
    loop-owner read can never blank the statusline.
    """
    try:
        from teatree.core.models import LoopLease  # noqa: PLC0415
        from teatree.loop.session_identity import current_session_id  # noqa: PLC0415
        from teatree.loop.statusline import loop_owner_anchor  # noqa: PLC0415

        status = LoopLease.objects.ownership_status("loop-owner")
        zone, line = loop_owner_anchor(status, current_session_id())
    except Exception:  # noqa: BLE001
        return
    if line:
        getattr(zones, zone).append(line)


def rerender_statusline(target: Path | None = None, *, colorize: bool | None = None) -> Path:
    """Re-render the statusline from current state without a full tick (#2625).

    Runs the idle (no-jobs) render path — the live-loop, open-PR, and loop-owner
    anchors over an empty zone set — so a stale merged-PR / terminal-ticket URL
    drops out of the rendered file. This is the idempotent self-heal seam the
    domain-layer ``StaleStatuslineEntryDetector`` cannot reach itself (it would
    invert the tach-enforced dependency DAG); the orchestration layer injects it
    as the action-ladder ``auto_fix_callable``, retiring the prior
    ``_default_rerender`` no-op.
    """
    zones = StatuslineZones()
    _populate_live_loops_in_anchors(zones, colorize=colorize)
    _write_open_prs_cache([], target=target)
    _populate_open_prs_in_anchors(zones, target=target, colorize=colorize)
    _populate_loop_owner_anchor(zones)
    return render(zones, target=target, colorize=colorize)


def self_improve_rerender(_report: object) -> None:
    """Action-ladder ``auto_fix_callable`` adapter for the statusline self-heal (#2625).

    Bridges the ladder's ``Callable[[DetectorReport], None]`` signature to the
    parameterless idle re-render. Every orchestration entry point that drives the
    cheap self-improve tier injects this as the ladder's ``auto_fix_callable`` — the
    dedicated ``loop_self_improve`` slot and the tick piggyback alike — so the
    domain-layer ``StaleStatuslineEntryDetector`` never reaches up into this
    orchestration render seam itself (which would invert the tach-enforced DAG).
    The ladder only invokes it once a whitelisted ``auto_fix`` report reaches the
    ``auto_fix`` rung; the report is unused here (the heal reads current state).
    """
    rerender_statusline()
