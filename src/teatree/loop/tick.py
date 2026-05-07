"""One tick of the fat loop: scan in parallel, dispatch, render statusline.

The ``run_tick`` entry point is what ``t3 loop tick`` invokes. The loop
slot itself just calls this function on a cadence; everything that needs
testing lives here as plain Python.
"""

import datetime as dt
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from teatree.backends.protocols import CodeHostBackend, MessagingBackend
from teatree.core.backend_factory import OverlayBackends
from teatree.loop.dispatch import DispatchAction, dispatch
from teatree.loop.scanners import (
    AssignedIssuesScanner,
    MyPrsScanner,
    NotionViewScanner,
    PendingTasksScanner,
    ReviewerPrsScanner,
    Scanner,
    SlackMentionsScanner,
)
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.notion_view import NotionLike
from teatree.loop.statusline import StatuslineEntry, StatuslineZones, render

logger = logging.getLogger(__name__)


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
class _ScannerJob:
    """Internal record pairing a scanner with its overlay tag."""

    scanner: Scanner
    overlay: str


@dataclass(frozen=True, slots=True)
class TickRequest:
    """What to scan in one tick — overlays, backends, or an explicit scanner list.

    Pass *backends* to scan many overlays in one tick. Pass *host*/*messaging*
    for the legacy single-overlay path. Pass *scanners* to bypass default
    scanner construction entirely (mostly used by tests).
    """

    scanners: list[Scanner] | None = None
    host: CodeHostBackend | None = None
    messaging: MessagingBackend | None = None
    backends: list[OverlayBackends] | None = None
    notion_client: NotionLike | None = None
    ready_labels: tuple[str, ...] = ()


def _run_job(job: _ScannerJob) -> tuple[str, list[ScanSignal], str]:
    label = f"{job.scanner.name}[{job.overlay}]" if job.overlay else job.scanner.name
    try:
        signals = job.scanner.scan()
        if job.overlay:
            signals = [
                ScanSignal(
                    kind=s.kind,
                    summary=s.summary,
                    payload={**s.payload, "overlay": job.overlay},
                )
                for s in signals
            ]
    except Exception as exc:
        logger.exception("Scanner %s raised", label)
        return label, [], f"{type(exc).__name__}: {exc}"
    return label, signals, ""


def build_default_jobs(
    *,
    backends: list[OverlayBackends] | None = None,
    host: CodeHostBackend | None = None,
    messaging: MessagingBackend | None = None,
    notion_client: NotionLike | None = None,
    ready_labels: tuple[str, ...] = (),
) -> list[_ScannerJob]:
    """Build the default scanner jobs from one or more overlays.

    Pass *backends* to scan multiple overlays in one tick (each gets its
    own host/messaging credentials). The legacy *host*/*messaging* shape
    is preserved for callers that resolve a single overlay themselves.
    """
    jobs: list[_ScannerJob] = [_ScannerJob(scanner=PendingTasksScanner(), overlay="")]

    if backends:
        for backend in backends:
            tag = backend.name
            if backend.host is not None:
                jobs.extend(
                    [
                        _ScannerJob(scanner=MyPrsScanner(host=backend.host), overlay=tag),
                        _ScannerJob(scanner=ReviewerPrsScanner(host=backend.host), overlay=tag),
                        _ScannerJob(
                            scanner=AssignedIssuesScanner(host=backend.host, ready_labels=backend.ready_labels),
                            overlay=tag,
                        ),
                    ]
                )
            if backend.messaging is not None:
                jobs.append(_ScannerJob(scanner=SlackMentionsScanner(backend=backend.messaging), overlay=tag))
    else:
        if host is not None:
            jobs.extend(
                [
                    _ScannerJob(scanner=MyPrsScanner(host=host), overlay=""),
                    _ScannerJob(scanner=ReviewerPrsScanner(host=host), overlay=""),
                    _ScannerJob(scanner=AssignedIssuesScanner(host=host, ready_labels=ready_labels), overlay=""),
                ]
            )
        if messaging is not None:
            jobs.append(_ScannerJob(scanner=SlackMentionsScanner(backend=messaging), overlay=""))

    if notion_client is not None:
        jobs.append(_ScannerJob(scanner=NotionViewScanner(client=notion_client), overlay=""))
    return jobs


def build_default_scanners(
    *,
    host: CodeHostBackend | None,
    messaging: MessagingBackend | None,
    notion_client: NotionLike | None = None,
    ready_labels: tuple[str, ...] = (),
) -> list[Scanner]:
    """Single-overlay scanner builder kept for tests and ad-hoc CLI use."""
    return [
        job.scanner
        for job in build_default_jobs(
            host=host,
            messaging=messaging,
            notion_client=notion_client,
            ready_labels=ready_labels,
        )
    ]


def _zones_for(actions: list[DispatchAction]) -> StatuslineZones:
    zones = StatuslineZones()
    for action in actions:
        url = action.payload.get("url") if isinstance(action.payload, dict) else None
        url_str = url if isinstance(url, str) else ""
        overlay = action.payload.get("overlay") if isinstance(action.payload, dict) else None
        prefix = f"[{overlay}] " if isinstance(overlay, str) and overlay else ""

        if action.kind == "statusline":
            zone_list = getattr(zones, action.zone, None)
            if isinstance(zone_list, list):
                zone_list.append(StatuslineEntry(text=f"{prefix}{action.detail}", url=url_str))
        else:  # "agent" or "webhook" — surface as in-flight progress
            text = f"→ {action.zone}: {prefix}{action.detail}"
            zones.in_flight.append(StatuslineEntry(text=text, url=url_str))
    return zones


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
            StatuslineZones(anchors=[_anchor_line(started_at)]),
            target=statusline_path,
            colorize=colorize,
        )
        return report

    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        for label, signals, error in pool.map(_run_job, jobs):
            report.signals.extend(signals)
            if error:
                report.errors[label] = error

    report.actions = dispatch(report.signals)

    zones = _zones_for(report.actions)
    zones.anchors.insert(0, _anchor_line(started_at))
    if report.errors:
        zones.action_needed.append(f"scanner errors: {', '.join(report.errors)}")
    report.statusline_path = render(zones, target=statusline_path, colorize=colorize)
    return report


def _anchor_line(started_at: dt.datetime) -> str:
    overlays = os.environ.get("T3_OVERLAY_NAME", "").strip()
    suffix = f"  ({overlays})" if overlays else ""
    return f"tick @ {started_at.isoformat(timespec='seconds')}{suffix}"
