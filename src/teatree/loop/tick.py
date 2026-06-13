"""One tick of the fat loop: scan in parallel, dispatch, render statusline.

The ``run_tick`` entry point is what ``t3 loop tick`` invokes. The loop
slot itself just calls this function on a cadence; everything that needs
testing lives here as plain Python.

Per-concern helpers live in sibling modules to keep this orchestrator
under the module-health LOC gate. ``scanner_factories`` / ``domain_jobs``
/ ``global_scanner_factories`` build scanner jobs,
``tick_recovery`` runs boot/tick recovery and post-dispatch
side-effects (mechanical handlers, agent dispatch persistence), and
``tick_freshness`` captures the repo-freshness snapshot for the
``tick-meta.json`` sidecar.

The names re-exported below are the public surface other modules and
tests rely on — keep the re-export list in sync with downstream
imports.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from teatree.config import (  # noqa: F401 — re-export kept live for test monkeypatch
    discover_overlays,
    get_effective_settings,
    load_config,
)
from teatree.core.backend_factory import OverlayBackends

# Re-exported for downstream importers. Tests monkeypatch
# ``teatree.loop.tick.load_config``/``discover_overlays``; keep the
# binding here so the legacy patch path stays live (the
# ``scanner_factories`` / ``global_scanner_factories`` modules have
# their own bindings patched by the test setup that exercises the moved
# functions).
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.loop.dispatch import DispatchAction
from teatree.loop.domain_jobs import _identity_groups_for_overlay, _run_job, jobs_for_domain
from teatree.loop.global_scanner_factories import build_default_jobs, build_default_scanners
from teatree.loop.job_identity import Domain, _ScannerJob
from teatree.loop.phases import act_phase, orchestrate_phase, scan_phase, sweep_phase
from teatree.loop.rendering import zones_for
from teatree.loop.scanner_factories import _jobs_for_backend_hosts
from teatree.loop.scanner_factory_config import (
    _gitlab_approvals_enabled,
    _user_identity_aliases_for_overlay,
    _user_slack_id_for_overlay,
)
from teatree.loop.scanners.base import Scanner, ScanSignal
from teatree.loop.scanners.notion_view import NotionLike
from teatree.loop.statusline import StatuslineZones, render
from teatree.loop.tick_freshness import (
    _canonical_overlay_names,
    _collect_repo_freshness,
    _repo_freshness,
    _repos_from_toml,
    _write_tick_meta,
)
from teatree.loop.tick_recovery import _execute_mechanical, _persist_agent_dispatches, _reap_stale_task_claims
from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host, _identity_alias_groups_for_overlay

logger = logging.getLogger(__name__)

__all__ = [
    "DispatchAction",
    "Domain",
    "ScanSignal",
    "TickReport",
    "TickRequest",
    "_ScannerJob",
    "_allowed_url_prefixes_for_host",
    "_canonical_overlay_names",
    "_collect_repo_freshness",
    "_execute_mechanical",
    "_gitlab_approvals_enabled",
    "_identity_alias_groups_for_overlay",
    "_jobs_for_backend_hosts",
    "_persist_agent_dispatches",
    "_reap_stale_task_claims",
    "_repo_freshness",
    "_repos_from_toml",
    "_run_job",
    "_user_identity_aliases_for_overlay",
    "_user_slack_id_for_overlay",
    "_write_tick_meta",
    "build_default_jobs",
    "build_default_scanners",
    "jobs_for_domain",
    "run_tick",
]


@dataclass(slots=True)
class TickReport:
    """Result of one tick — for tests and the ``t3 loop status`` command."""

    started_at: dt.datetime
    signals: list[ScanSignal] = field(default_factory=list)
    actions: list[DispatchAction] = field(default_factory=list)
    statusline_path: Path | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    @property
    def action_count(self) -> int:
        return len(self.actions)


@dataclass(frozen=True, slots=True)
class TickRequest:
    """What to scan in one tick — overlays, backends, or an explicit scanner list.

    Pass *backends* to scan many overlays in one tick. Pass *host*/*messaging*
    for the single-overlay path. Pass *scanners* to bypass default
    scanner construction entirely (mostly used by tests).
    """

    scanners: list[Scanner] | None = None
    host: CodeHostBackend | None = None
    messaging: MessagingBackend | None = None
    backends: list[OverlayBackends] | None = None
    notion_client: NotionLike | None = None
    ready_labels: tuple[str, ...] = ()


type JobsBuilder = Callable[[TickRequest, dt.datetime], list[_ScannerJob]]


def run_tick(
    request: TickRequest | None = None,
    *,
    statusline_path: Path | None = None,
    colorize: bool | None = None,
    now: dt.datetime | None = None,
    jobs_builder: JobsBuilder | None = None,
) -> TickReport:
    """Run all scanners in parallel, dispatch, render statusline, return report.

    *request.backends* takes priority over *request.host*/*messaging*:
    passing it scans every listed overlay in one tick and prefixes signals
    with the overlay name. Falls back to a single-overlay scan when only
    *host*/*messaging* are provided. *now* and *statusline_path* are test
    overrides. *colorize* defaults to ``True`` unless ``NO_COLOR`` is set
    in the environment.

    *jobs_builder* is the source of scanner jobs for the no-``scanners``
    path. The ``loop_tick`` management command injects the registry-driven
    fan-out (:func:`teatree.loops.fanout.build_registry_jobs`) so the
    mini-loop registry is the single source of which scanners run a live
    tick (#1481); the default falls back to :func:`build_default_jobs`.
    The seam keeps :mod:`teatree.loop` from importing :mod:`teatree.loops`
    up-stack.

    The body composes the named tick phases (#1796): :func:`sweep_phase`
    splits the maintenance scanners out of the world-scan, :func:`scan_phase`
    fans both slices out in parallel and merges their signals,
    :func:`act_phase` dispatches + runs mechanical handlers + persists
    agent dispatches, and :func:`orchestrate_phase` runs the speed-driven
    autonomous fan-out (a no-op at the default ``medium`` speed).
    """
    request = request or TickRequest()
    started_at = now or dt.datetime.now(dt.UTC)
    _reap_stale_task_claims()
    if request.scanners is not None:
        jobs = [_ScannerJob(scanner=s, overlay="") for s in request.scanners]
    elif jobs_builder is not None:
        jobs = jobs_builder(request, started_at)
    else:
        jobs = build_default_jobs(
            backends=request.backends,
            host=request.host,
            messaging=request.messaging,
            notion_client=request.notion_client,
            ready_labels=request.ready_labels,
        )
    report = TickReport(started_at=started_at)

    if not jobs:
        empty_zones = StatuslineZones()
        _populate_live_loops_in_anchors(empty_zones, colorize=colorize)
        _write_open_prs_cache(report.signals, target=statusline_path)
        _populate_open_prs_in_anchors(empty_zones, target=statusline_path, colorize=colorize)
        _populate_loop_owner_anchor(empty_zones)
        report.statusline_path = render(
            empty_zones,
            target=statusline_path,
            colorize=colorize,
        )
        _write_tick_meta(started_at, target=statusline_path)
        return report

    split = sweep_phase(jobs)
    outcome = scan_phase(split.scan_jobs + split.sweep_jobs)
    report.signals.extend(outcome.signals)
    report.errors.update(outcome.errors)

    act_phase(report)

    zones = zones_for(report.actions, colorize=colorize, identity_aliases=_identity_aliases_for_request(request))
    _write_tick_meta(started_at, target=statusline_path)
    # #1796 (WI-1): plan the admit budget AFTER the freshness header write —
    # the planner MERGES its key into tick-meta.json, so running it after
    # ``_write_tick_meta`` (a full overwrite) keeps both. Never claims in the
    # tick: the live ``claim_next`` is the single claim point.
    _orchestrate(request, statusline_path=statusline_path)
    _write_open_prs_cache(report.signals, target=statusline_path)
    _populate_open_prs_in_anchors(zones, target=statusline_path, colorize=colorize)
    if report.errors:
        zones.action_needed.append(f"scanner errors: {', '.join(report.errors)}")
    _populate_loop_owner_anchor(zones)
    report.statusline_path = render(zones, target=statusline_path, colorize=colorize)
    return report


def _orchestrate(request: TickRequest, *, statusline_path: Path | None) -> None:
    """Plan the admit BUDGET — the read-only fan-out ceiling (#1796, WI-1).

    The reconciled fan-out keeps exactly ONE claim point: the live
    ``claim_next`` CAS. ``orchestrate_phase`` is a read-only PLANNER here — it
    never claims in the tick (the old ``claim=True`` arm orphaned claims the
    live claimer also took). Instead it computes the clamped fan-out cap and
    persists it as a BUDGET ceiling to the tick-meta sidecar for the live
    claimer to read.

    The ``[teatree] orchestrate_claim_enabled`` toggle (existing accessor,
    default OFF) gates the planner:

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
    """Resolve the ``orchestrate_claim_enabled`` toggle; fail OFF (#1796).

    Reads the effective setting (env -> DB -> per-overlay -> global -> default).
    Any settings-read error degrades to ``False`` so the dormant ``claim=False``
    path is the fail-safe — the arm can never fire on a broken config read.
    """
    try:
        return get_effective_settings().orchestrate_claim_enabled
    except Exception:  # noqa: BLE001
        return False


def _identity_aliases_for_request(request: TickRequest) -> tuple[tuple[str, ...], ...]:
    """Union the identity-alias groups across every overlay in *request*.

    The renderer suppresses a reassignment between two handles of the same
    human and collapses each handle to its group's canonical name. Fails
    open: any config-read error degrades to no suppression.

    Routes through :func:`_identity_groups_for_overlay` (not the raw
    resolver) so the #1113 self-group fallback applies on the render path
    too: when no explicit ``identity_aliases`` is configured, the operator's
    ``backend.identities`` (← ``user_identity_aliases``; the accessor #1773's
    ``TrustedIdentity`` will sit behind) form one implicit self-group.
    Without it the render-side group was empty and intra-self reassigns
    leaked as ``reassigned`` churn.
    """
    groups: list[tuple[str, ...]] = []
    try:
        for backend in request.backends or []:
            groups.extend(_identity_groups_for_overlay(backend))
    except Exception:  # noqa: BLE001
        return ()
    return tuple(groups)


def _populate_live_loops_in_anchors(zones: StatuslineZones, *, colorize: bool | None = None) -> None:
    """Append one anchor line per live LoopLease row (#1156).

    Used by the empty-jobs path in :func:`run_tick` so even an idle tick
    still surfaces the running loops. The non-empty path goes through
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


def _write_open_prs_cache(signals: list[ScanSignal], *, target: Path | None) -> None:
    """Snapshot the tick's open PRs to the ``open-prs.json`` sidecar (#271).

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
    """Append the open-PR summary anchor (#271).

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
    """Append the #1073 foreign-hijack loop-owner RED line.

    The live-loops anchor (the single dedicated loop line folding all live
    LoopLease rows) is populated separately by
    :func:`teatree.loop.rendering._populate_live_loops_anchor` (#1163, #1184, #130).
    This function is responsible only for the #1073 foreign-hijack RED line
    surfaced when a different live session holds ``loop-owner``.

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
