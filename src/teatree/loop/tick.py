"""One tick of the fat loop: scan in parallel, dispatch, render statusline.

The ``run_tick`` entry point is what ``t3 loop tick`` invokes. The loop
slot itself just calls this function on a cadence; everything that needs
testing lives here as plain Python.
"""

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from teatree.backends.protocols import CodeHostBackend, MessagingBackend
from teatree.loop.dispatch import DispatchAction, dispatch
from teatree.loop.scanners import (
    MyPrsScanner,
    PendingTasksScanner,
    ReviewChannelsScanner,
    ReviewerPrsScanner,
    Scanner,
    SlackMentionsScanner,
)
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.statusline import StatuslineZones, render

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


def _run_scanner(scanner: Scanner) -> tuple[str, list[ScanSignal], str]:
    try:
        return scanner.name, scanner.scan(), ""
    except Exception as exc:
        logger.exception("Scanner %s raised", scanner.name)
        return scanner.name, [], f"{type(exc).__name__}: {exc}"


def build_default_scanners(
    *,
    host: CodeHostBackend | None,
    messaging: MessagingBackend | None,
) -> list[Scanner]:
    """Construct the default scanner set from the active overlay's backends."""
    scanners: list[Scanner] = [PendingTasksScanner()]
    if host is not None:
        scanners.extend([MyPrsScanner(host=host), ReviewerPrsScanner(host=host)])
    if messaging is not None:
        scanners.extend([SlackMentionsScanner(backend=messaging), ReviewChannelsScanner(backend=messaging)])
    return scanners


def _zones_for(actions: list[DispatchAction]) -> StatuslineZones:
    zones = StatuslineZones()
    for action in actions:
        if action.kind == "statusline":
            target = getattr(zones, action.zone, None)
            if isinstance(target, list):
                target.append(action.detail)
            continue
        if action.kind == "agent":
            zones.in_flight.append(f"→ {action.zone}: {action.detail}")
            continue
        if action.kind == "webhook":
            zones.in_flight.append(f"→ {action.zone}: {action.detail}")
            continue
        if action.kind == "ticket_create":
            zones.action_needed.append(action.detail)
    return zones


def run_tick(
    scanners: list[Scanner] | None = None,
    *,
    host: CodeHostBackend | None = None,
    messaging: MessagingBackend | None = None,
    statusline_path: Path | None = None,
    now: dt.datetime | None = None,
) -> TickReport:
    """Run all scanners in parallel, dispatch, render statusline, return report.

    *now* is overridable for tests. *statusline_path* override is forwarded
    to the renderer; ``None`` uses the default location.
    """
    started_at = now or dt.datetime.now(dt.UTC)
    scanners = scanners if scanners is not None else build_default_scanners(host=host, messaging=messaging)
    report = TickReport(started_at=started_at)

    if not scanners:
        report.statusline_path = render(StatuslineZones(anchors=[_anchor_line(started_at)]), target=statusline_path)
        return report

    with ThreadPoolExecutor(max_workers=max(1, len(scanners))) as pool:
        for scanner_name, signals, error in pool.map(_run_scanner, scanners):
            report.signals.extend(signals)
            if error:
                report.errors[scanner_name] = error

    report.actions = dispatch(report.signals)

    zones = _zones_for(report.actions)
    zones.anchors.insert(0, _anchor_line(started_at))
    if report.errors:
        zones.action_needed.append(f"scanner errors: {', '.join(report.errors)}")
    report.statusline_path = render(zones, target=statusline_path)
    return report


def _anchor_line(started_at: dt.datetime) -> str:
    return f"tick @ {started_at.isoformat(timespec='seconds')}"
