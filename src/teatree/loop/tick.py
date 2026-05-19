"""One tick of the fat loop: scan in parallel, dispatch, render statusline.

The ``run_tick`` entry point is what ``t3 loop tick`` invokes. The loop
slot itself just calls this function on a cadence; everything that needs
testing lives here as plain Python.

Per-concern helpers live in sibling modules to keep this orchestrator
under the module-health LOC gate. ``tick_jobs`` builds scanner jobs,
``tick_recovery`` runs boot/tick recovery and post-dispatch
side-effects (mechanical handlers, agent dispatch persistence,
dashboard recording), and ``tick_freshness`` captures the repo-
freshness snapshot for the ``tick-meta.json`` sidecar.

The names re-exported below are the public surface other modules and
tests rely on — keep the re-export list in sync with downstream
imports.
"""

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

# Re-exported for downstream importers. Tests monkeypatch
# ``teatree.loop.tick.load_config``/``discover_overlays``; keep the
# binding here so the legacy patch path stays live (the tick_jobs
# module has its own binding patched by the test setup that exercises
# the moved functions).
from teatree.backends.protocols import CodeHostBackend, MessagingBackend
from teatree.config import discover_overlays, load_config  # noqa: F401
from teatree.core.backend_factory import OverlayBackends
from teatree.loop.dispatch import DispatchAction, dispatch
from teatree.loop.rendering import zones_for
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
from teatree.loop.tick_jobs import (
    _gitlab_approvals_enabled,
    _jobs_for_backend_hosts,
    _run_job,
    _ScannerJob,
    _user_identity_aliases_for_overlay,
    _user_slack_id_for_overlay,
    build_default_jobs,
    build_default_scanners,
)
from teatree.loop.tick_recovery import (
    _execute_mechanical,
    _persist_agent_dispatches,
    _reap_stale_task_claims,
    _record_dashboard_actions,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DispatchAction",
    "ScanSignal",
    "TickReport",
    "TickRequest",
    "_ScannerJob",
    "_canonical_overlay_names",
    "_collect_repo_freshness",
    "_execute_mechanical",
    "_gitlab_approvals_enabled",
    "_jobs_for_backend_hosts",
    "_persist_agent_dispatches",
    "_reap_stale_task_claims",
    "_record_dashboard_actions",
    "_repo_freshness",
    "_repos_from_toml",
    "_run_job",
    "_user_identity_aliases_for_overlay",
    "_user_slack_id_for_overlay",
    "_write_tick_meta",
    "build_default_jobs",
    "build_default_scanners",
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


def run_tick(
    request: TickRequest | None = None,
    *,
    statusline_path: Path | None = None,
    colorize: bool | None = None,
    now: dt.datetime | None = None,
) -> TickReport:
    """Run all scanners in parallel, dispatch, render statusline, return report.

    *request.backends* takes priority over *request.host*/*messaging*:
    passing it scans every listed overlay in one tick and prefixes signals
    with the overlay name. Falls back to a single-overlay scan when only
    *host*/*messaging* are provided. *now* and *statusline_path* are test
    overrides. *colorize* defaults to ``True`` unless ``NO_COLOR`` is set
    in the environment.
    """
    request = request or TickRequest()
    started_at = now or dt.datetime.now(dt.UTC)
    _reap_stale_task_claims()
    if request.scanners is not None:
        jobs = [_ScannerJob(scanner=s, overlay="") for s in request.scanners]
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
        report.statusline_path = render(
            StatuslineZones(),
            target=statusline_path,
            colorize=colorize,
        )
        _write_tick_meta(started_at, target=statusline_path)
        return report

    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        for label, signals, error in pool.map(_run_job, jobs):
            report.signals.extend(signals)
            if error:
                report.errors[label] = error

    report.actions = dispatch(report.signals)
    _execute_mechanical(report)
    _persist_agent_dispatches(report)
    _record_dashboard_actions(report, started_at)

    zones = zones_for(report.actions, colorize=colorize)
    _write_tick_meta(started_at, target=statusline_path)
    if report.errors:
        zones.action_needed.append(f"scanner errors: {', '.join(report.errors)}")
    report.statusline_path = render(zones, target=statusline_path, colorize=colorize)
    return report
