"""One loop tick as a thin pipeline of named phases.

The ``run_tick`` entry point is what ``t3 loop tick`` invokes. It carries no
work inline: it resolves the tick's scanner jobs, then composes the named
phases in :mod:`teatree.loop.phases` — ``sweep`` then ``scan`` then ``act``
then ``render``. Per-concern helpers live in sibling modules:
``scanner_factories`` / ``domain_jobs`` / ``global_scanner_factories`` build
scanner jobs, ``tick_recovery`` runs boot/tick recovery and post-dispatch
side-effects, and ``tick_freshness`` captures the repo-freshness snapshot for
the ``tick-meta.json`` sidecar.

The names re-exported below are the public surface other modules and tests
rely on — keep the re-export list in sync with downstream imports.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from teatree.config import discover_overlays, load_config  # noqa: F401 — re-export kept live for test monkeypatch
from teatree.core.backend_factory import OverlayBackends

# Re-exported for downstream importers. Tests monkeypatch
# ``teatree.loop.tick.load_config``/``discover_overlays``; keep the
# binding here so the legacy patch path stays live (the
# ``scanner_factories`` / ``global_scanner_factories`` modules have
# their own bindings patched by the test setup that exercises the moved
# functions).
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.loop.dispatch import DispatchAction
from teatree.loop.domain_jobs import _run_job, jobs_for_domain
from teatree.loop.global_scanner_factories import build_default_jobs, build_default_scanners
from teatree.loop.job_identity import Domain, _ScannerJob
from teatree.loop.phases import act_phase, render_phase, scan_phase, sweep_phase
from teatree.loop.scanner_factories import _jobs_for_backend_hosts
from teatree.loop.scanner_factory_config import (
    _gitlab_approvals_enabled,
    _user_identity_aliases_for_overlay,
    _user_slack_id_for_overlay,
)
from teatree.loop.scanners.base import Scanner, ScanSignal
from teatree.loop.scanners.notion_view import NotionLike
from teatree.loop.tick_freshness import (
    _canonical_overlay_names,
    _collect_repo_freshness,
    _repo_freshness,
    _repos_from_toml,
    _write_tick_meta,
)
from teatree.loop.tick_recovery import _execute_mechanical, _persist_agent_dispatches, _reap_stale_task_claims
from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host, _identity_alias_groups_for_overlay

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
    """Run one loop tick as a thin pipeline of named phases.

    Resolves the tick's scanner jobs, then composes the phases:
    :func:`~teatree.loop.phases.sweep_phase` splits the maintenance scanners
    out of the world-scan, :func:`~teatree.loop.phases.scan_phase` fans both
    slices out in parallel and merges their signals,
    :func:`~teatree.loop.phases.act_phase` dispatches them into actions and
    runs the inline mechanical handlers, and
    :func:`~teatree.loop.phases.render_phase` writes the statusline and
    sidecars (planning the admit budget on the way). An empty job set skips
    straight to an idle render.

    *request.backends* takes priority over *request.host*/*messaging*: passing
    it scans every listed overlay in one tick and prefixes signals with the
    overlay name. *now* and *statusline_path* are test overrides; *colorize*
    defaults to ``True`` unless ``NO_COLOR`` is set. *jobs_builder* is the
    source of scanner jobs for the no-``scanners`` path: the ``loops_tick``
    per-loop command injects the DB ``Loop``-table fan-out
    (:func:`teatree.loops.loop_table.build_loop_table_jobs`) so each enabled,
    due ``Loop`` row is the single source of which scanners run a live tick;
    the default falls back to :func:`build_default_jobs`. The seam keeps
    :mod:`teatree.loop` from importing :mod:`teatree.loops` up-stack.
    """
    request = request or TickRequest()
    started_at = now or dt.datetime.now(dt.UTC)
    report = TickReport(started_at=started_at)
    # Recover BEFORE scanners fan out, recording any sweep failure into the report so a
    # broken recovery surfaces in action_needed instead of freezing the factory silently.
    _reap_stale_task_claims(report.errors)
    jobs = _resolve_jobs(request, started_at, jobs_builder)
    if jobs:
        split = sweep_phase(jobs)
        outcome = scan_phase(split.scan_jobs + split.sweep_jobs)
        report.signals.extend(outcome.signals)
        report.errors.update(outcome.errors)
        act_phase(report)
    render_phase(
        report,
        request,
        jobs=jobs,
        statusline_path=statusline_path,
        colorize=colorize,
    )
    return report


def _resolve_jobs(
    request: TickRequest,
    started_at: dt.datetime,
    jobs_builder: JobsBuilder | None,
) -> list[_ScannerJob]:
    """Pick the tick's scanner jobs: explicit scanners, an injected builder, or the default fan-out."""
    if request.scanners is not None:
        return [_ScannerJob(scanner=s, overlay="") for s in request.scanners]
    if jobs_builder is not None:
        return jobs_builder(request, started_at)
    return build_default_jobs(
        backends=request.backends,
        host=request.host,
        messaging=request.messaging,
        notion_client=request.notion_client,
    )
