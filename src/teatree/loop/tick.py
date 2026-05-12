"""One tick of the fat loop: scan in parallel, dispatch, render statusline.

The ``run_tick`` entry point is what ``t3 loop tick`` invokes. The loop
slot itself just calls this function on a cadence; everything that needs
testing lives here as plain Python.
"""

import datetime as dt
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from teatree.backends.protocols import CodeHostBackend, MessagingBackend
from teatree.core.backend_factory import OverlayBackends
from teatree.loop.dispatch import ActionPayload, DispatchAction, dispatch
from teatree.loop.scanners import (
    ActiveTicketsScanner,
    AssignedIssuesScanner,
    MyPrsScanner,
    NotionViewScanner,
    PendingTasksScanner,
    ReviewerPrsScanner,
    Scanner,
    SlackMentionsScanner,
    TicketCompletionScanner,
    TicketDispositionScanner,
)
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.notion_view import NotionLike
from teatree.loop.statusline import StatuslineEntry, StatuslineZones, _hyperlink, render

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
            jobs.append(_ScannerJob(scanner=ActiveTicketsScanner(overlay_name=tag), overlay=tag))
            if backend.host is not None:
                jobs.extend(
                    [
                        _ScannerJob(scanner=MyPrsScanner(host=backend.host), overlay=tag),
                        _ScannerJob(scanner=ReviewerPrsScanner(host=backend.host), overlay=tag),
                        _ScannerJob(
                            scanner=AssignedIssuesScanner(
                                host=backend.host,
                                ready_labels=backend.ready_labels,
                                exclude_labels=backend.exclude_labels,
                                auto_start=backend.auto_start_assigned_issues,
                                max_concurrent=backend.max_concurrent_auto_starts,
                                overlay_name=tag,
                            ),
                            overlay=tag,
                        ),
                        _ScannerJob(
                            scanner=TicketDispositionScanner(
                                host=backend.host,
                                overlay=backend.overlay,
                                ready_labels=backend.ready_labels,
                                overlay_name=tag,
                            ),
                            overlay=tag,
                        ),
                    ],
                )
                if backend.overlay is not None:
                    jobs.append(
                        _ScannerJob(
                            scanner=TicketCompletionScanner(
                                overlay=backend.overlay,
                                overlay_name=tag,
                            ),
                            overlay=tag,
                        ),
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
                ],
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


_DISPOSITION_LABELS: dict[str, str] = {
    "issue_closed": "closed issues",
    "unassigned": "reassigned away",
    "label_removed": "ready-label removed",
}


@dataclass(frozen=True, slots=True)
class _PRRef:
    iid: int
    url: str
    annotation: str


def _pr_ref(action: DispatchAction) -> _PRRef | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    iid = payload.get("iid")
    if not isinstance(iid, int) or iid == 0:
        return None
    url = payload.get("url", "")
    draft_count = payload.get("draft_count")
    status = payload.get("status", "")
    if isinstance(draft_count, int) and draft_count > 0:
        return _PRRef(iid=iid, url=url, annotation=f"{draft_count} notes")
    if status in {"failed", "failure", "error"}:
        return _PRRef(iid=iid, url=url, annotation=f"pipeline {status}")
    return _PRRef(iid=iid, url=url, annotation="")


def _render_pr_group(overlay: str, refs: list[_PRRef]) -> str:
    prefix = f"[{overlay}] " if overlay else ""
    parts: list[str] = []
    for ref in refs:
        label = f"!{ref.iid}"
        if ref.annotation:
            label += f" ({ref.annotation})"
        parts.append(_hyperlink(label, ref.url) if ref.url else label)
    return f"{prefix}{' · '.join(parts)}"


@dataclass(slots=True)
class _ClassifiedActions:
    disposition_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    ready_counts: dict[str, int] = field(default_factory=dict)
    action_prs: dict[str, list[_PRRef]] = field(default_factory=dict)
    inflight_prs: dict[str, list[_PRRef]] = field(default_factory=dict)
    active_tickets: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    other: list[tuple[str, StatuslineEntry]] = field(default_factory=list)


def _classify_actions(actions: list[DispatchAction]) -> _ClassifiedActions:
    c = _ClassifiedActions()
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        url_str = payload.get("url", "") if isinstance(payload.get("url"), str) else ""
        overlay = payload.get("overlay", "") if isinstance(payload.get("overlay"), str) else ""
        prefix = f"[{overlay}] " if overlay else ""

        if action.kind == "statusline":
            state = payload.get("state")
            ticket_number = payload.get("ticket_number")
            if action.zone == "anchors" and isinstance(state, str) and isinstance(ticket_number, str):
                c.active_tickets.setdefault(overlay, []).append((ticket_number, state))
            elif isinstance((reason := payload.get("reason")), str):
                c.disposition_counts.setdefault(overlay, {}).setdefault(reason, 0)
                c.disposition_counts[overlay][reason] += 1
            elif action.zone == "action_needed" and action.detail.startswith("Ready to start:"):
                c.ready_counts[overlay] = c.ready_counts.get(overlay, 0) + 1
            elif (ref := _pr_ref(action)) is not None:
                bucket = c.action_prs if action.zone == "action_needed" else c.inflight_prs
                bucket.setdefault(overlay, []).append(ref)
            else:
                c.other.append((action.zone, StatuslineEntry(text=f"{prefix}{action.detail}", url=url_str)))
        elif action.kind == "mechanical":
            c.other.append(("in_flight", StatuslineEntry(text=f"⚙ {prefix}{action.detail}", url=url_str)))
        else:
            text = f"→ {action.zone}: {prefix}{action.detail}"
            c.other.append(("in_flight", StatuslineEntry(text=text, url=url_str)))
    return c


def _zones_for(actions: list[DispatchAction]) -> StatuslineZones:
    zones = StatuslineZones()
    c = _classify_actions(actions)

    for overlay_key, tickets in sorted(c.active_tickets.items()):
        prefix = f"[{overlay_key}] " if overlay_key else ""
        parts = [f"#{num} {state}" for num, state in tickets]
        zones.anchors.append(f"{prefix}{' · '.join(parts)}")

    for overlay_key, refs in sorted(c.action_prs.items()):
        zones.action_needed.append(_render_pr_group(overlay_key, refs))

    for overlay_key, reasons in sorted(c.disposition_counts.items()):
        prefix = f"[{overlay_key}] " if overlay_key else ""
        parts = [f"{count} {_DISPOSITION_LABELS.get(r, r)}" for r, count in reasons.items()]
        zones.action_needed.append(f"{prefix}Stale tickets: {', '.join(parts)}")

    for overlay_key, count in sorted(c.ready_counts.items()):
        prefix = f"[{overlay_key}] " if overlay_key else ""
        zones.action_needed.append(f"{prefix}{count} issues ready to start")

    for overlay_key, refs in sorted(c.inflight_prs.items()):
        zones.in_flight.append(_render_pr_group(overlay_key, refs))

    for zone_name, entry in c.other:
        zone_list = getattr(zones, zone_name, None)
        if isinstance(zone_list, list):
            zone_list.append(entry)

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

    zones = _zones_for(report.actions)
    _write_tick_meta(started_at, target=statusline_path)
    if report.errors:
        zones.action_needed.append(f"scanner errors: {', '.join(report.errors)}")
    report.statusline_path = render(zones, target=statusline_path, colorize=colorize)
    return report


def _execute_mechanical(report: TickReport) -> None:
    """Execute inline mechanical actions (ticket completions, etc.).

    Runs after dispatch but before statusline render so the statusline
    reflects the post-transition state. Errors are captured in
    ``report.errors`` — they never abort the tick.
    """
    for action in report.actions:
        if action.kind != "mechanical":
            continue
        handler = _MECHANICAL_HANDLERS.get(action.zone)
        if handler is not None:
            try:
                handler(action.payload)
            except Exception as exc:
                label = f"{action.zone}[{action.payload.get('ticket_id', '?')}]"
                logger.exception("Mechanical action %s failed", label)
                report.errors[label] = f"{type(exc).__name__}: {exc}"


def _ignore_disposed_ticket(payload: ActionPayload) -> None:
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    ticket = ticket_model.objects.get(pk=ticket_id)
    if hasattr(ticket, "ignore"):
        ticket.ignore()
        ticket.save()
        logger.info("Auto-ignored ticket %s (reason: %s)", ticket_id, payload.get("reason", "?"))


def _complete_ticket(payload: ActionPayload) -> None:
    """Transition a ticket from its current post-ship state toward delivered.

    FSM path: shipped → request_review → mark_merged → retrospect.
    Each step advances the ticket one state; ``mark_merged`` and
    ``retrospect`` enqueue workers via ``on_commit`` for teardown and
    retro I/O respectively.
    """
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    ticket = ticket_model.objects.get(pk=ticket_id)

    if ticket.state == "shipped":
        ticket.request_review()
        ticket.save()
    if ticket.state == "in_review":
        ticket.mark_merged()
        ticket.save()
    if ticket.state == "merged":
        ticket.retrospect()
        ticket.save()


_MECHANICAL_HANDLERS: dict[str, Callable[[ActionPayload], None]] = {
    "ticket_disposition": _ignore_disposed_ticket,
    "ticket_completion": _complete_ticket,
}


def _repo_freshness(repo_path: Path) -> dict[str, int | str] | None:
    from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415

    git_dir = repo_path / ".git"
    if not git_dir.exists():
        return None
    result = run_allowed_to_fail(
        ["git", "rev-list", "HEAD..origin/main", "--count"],
        cwd=repo_path,
        expected_codes=None,
        timeout=5,
    )
    try:
        behind = int(result.stdout.strip()) if result.returncode == 0 else -1
    except ValueError:
        behind = -1
    fetch_head = git_dir / "FETCH_HEAD"
    fetch_epoch = int(fetch_head.stat().st_mtime) if fetch_head.is_file() else 0
    return {"behind": behind, "fetch_epoch": fetch_epoch}


def _collect_repo_freshness() -> dict[str, dict[str, int | str]]:
    import tomllib  # noqa: PLC0415

    repos: dict[str, Path] = {}
    t3_repo = os.environ.get("T3_REPO")
    if t3_repo:
        repos["t3"] = Path(t3_repo).expanduser()
    toml_path = Path.home() / ".teatree.toml"
    if toml_path.is_file():
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            data = {}
        for name, overlay in (data.get("overlays") or {}).items():
            if isinstance(overlay, dict) and "path" in overlay:
                repos[name] = Path(str(overlay["path"])).expanduser()
    result: dict[str, dict[str, int | str]] = {}
    for label, path in repos.items():
        info = _repo_freshness(path)
        if info is not None:
            result[label] = info
    return result


def _write_tick_meta(started_at: dt.datetime, *, target: Path | None = None) -> None:
    from teatree.loop.statusline import default_path  # noqa: PLC0415

    meta_path = (target or default_path()).with_name("tick-meta.json")
    cadence = int(os.environ.get("T3_LOOP_CADENCE", "720") or "720")
    next_epoch = int(started_at.timestamp()) + cadence
    import json  # noqa: PLC0415

    freshness = _collect_repo_freshness()
    meta_path.write_text(
        json.dumps({"next_epoch": next_epoch, "cadence": cadence, "freshness": freshness}) + "\n",
        encoding="utf-8",
    )
