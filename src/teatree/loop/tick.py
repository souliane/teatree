"""One tick of the fat loop: scan in parallel, dispatch, render statusline.

The ``run_tick`` entry point is what ``t3 loop tick`` invokes. The loop
slot itself just calls this function on a cadence; everything that needs
testing lives here as plain Python.
"""

import datetime as dt
import json
import logging
import os
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from teatree.backends.protocols import CodeHostBackend, MessagingBackend
from teatree.config import discover_overlays, load_config
from teatree.core.backend_factory import OverlayBackends
from teatree.loop.dispatch import DispatchAction, dispatch
from teatree.loop.rendering import zones_for
from teatree.loop.scanners import (
    ActiveTicketsScanner,
    AssignedIssuesScanner,
    GitLabApprovalsScanner,
    IncomingEventsScanner,
    MyPrsScanner,
    NotionViewScanner,
    PendingTasksScanner,
    ReviewerPrsScanner,
    ReviewNagScanner,
    Scanner,
    SlackDmInboundScanner,
    SlackMentionsScanner,
    StaleTicketsScanner,
    TicketCompletionScanner,
    TicketDispositionScanner,
)
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.notion_view import NotionLike
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


@dataclass(frozen=True, slots=True)
class _ScannerJob:
    """Internal record pairing a scanner with its overlay tag."""

    scanner: Scanner
    overlay: str


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


def _jobs_for_backend_hosts(backend: OverlayBackends, tag: str) -> list[_ScannerJob]:
    """Build one scanner-job fan-out per host on *backend* (#976).

    Pre-fix the caller assumed one ``backend.host``; with multi-host the
    same fan-out must run for each platform that resolved a credential.
    ``TicketCompletionScanner`` is overlay-scoped (reads local Ticket
    rows), so it's emitted exactly once even when two hosts are present.
    """
    jobs: list[_ScannerJob] = []
    ticket_completion_emitted = False
    gitlab_approvals_enabled = _gitlab_approvals_enabled()
    for code_host in backend.hosts:
        jobs.extend(
            [
                _ScannerJob(
                    scanner=MyPrsScanner(host=code_host, identities=backend.identities),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=ReviewerPrsScanner(
                        host=code_host,
                        identities=backend.identities,
                        overlay_name=tag,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=AssignedIssuesScanner(
                        host=code_host,
                        ready_labels=backend.ready_labels,
                        exclude_labels=backend.exclude_labels,
                        auto_start=backend.auto_start_assigned_issues,
                        max_concurrent=backend.max_concurrent_auto_starts,
                        overlay_name=tag,
                        identities=backend.identities,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=TicketDispositionScanner(
                        host=code_host,
                        overlay=backend.overlay,
                        ready_labels=backend.ready_labels,
                        overlay_name=tag,
                        user_identity_aliases=_user_identity_aliases_for_overlay(tag),
                    ),
                    overlay=tag,
                ),
            ],
        )
        if backend.overlay is not None and not ticket_completion_emitted:
            jobs.append(
                _ScannerJob(
                    scanner=TicketCompletionScanner(
                        overlay=backend.overlay,
                        overlay_name=tag,
                    ),
                    overlay=tag,
                ),
            )
            ticket_completion_emitted = True
        if gitlab_approvals_enabled:
            # Poll-driven complement to the webhook-driven `SCHEDULE_MERGE` path
            # (#936). Off by default — opt-in via the env flag so deployments
            # that already wire the GitLab webhook do not double-emit.
            jobs.append(
                _ScannerJob(
                    scanner=GitLabApprovalsScanner(host=code_host, identities=backend.identities),
                    overlay=tag,
                ),
            )
    return jobs


def _gitlab_approvals_enabled() -> bool:
    """Read the ``TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED`` feature flag.

    Default off — the scanner is poll-driven and overlaps with the webhook
    path; deployments that already wire ``/hooks/gitlab/`` do not need it.
    Returns True for any truthy value (``1``, ``true``, ``yes``,
    case-insensitive); anything else (unset, ``0``, ``false``) is off.
    """
    raw = os.environ.get("TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


def _user_slack_id_for_overlay(overlay_name: str) -> str:
    """Resolve ``slack_user_id`` for the active overlay (overlay → global → empty).

    Used by :class:`ReviewNagScanner` to know where to DM long-stale MR
    warnings. Reads ``~/.teatree.toml`` directly so a fresh tick picks up
    a runtime config change without requiring an overlay reload.
    """
    try:
        toml_path = Path.home() / ".teatree.toml"
        if not toml_path.is_file():
            return ""
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    overlays = data.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        user_id = overlays[overlay_name].get("slack_user_id", "")
        if user_id:
            return str(user_id)
    teatree_cfg = data.get("teatree") or {}
    return str(teatree_cfg.get("slack_user_id", ""))


def _user_identity_aliases_for_overlay(overlay_name: str) -> tuple[str, ...]:
    """Resolve ``user_identity_aliases`` honouring any per-overlay override.

    The active overlay's ``[overlays.<name>]`` table wins over the global
    ``[teatree]`` value; with no setting anywhere we return the empty
    tuple so the disposition scanner keeps its legacy behaviour.
    """
    try:
        global_value = tuple(load_config().user.user_identity_aliases)
        if overlay_name:
            for entry in discover_overlays():
                if entry.name == overlay_name:
                    override = entry.overrides.get("user_identity_aliases")
                    if override is not None:
                        return tuple(str(s) for s in override)
                    break
    except Exception:  # noqa: BLE001 — never break a tick on a config read.
        logger.warning("Failed to resolve user_identity_aliases for %r; defaulting to empty", overlay_name)
        return ()
    return global_value


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
    own host/messaging credentials). The *host*/*messaging* shape
    is preserved for callers that resolve a single overlay themselves.
    """
    jobs: list[_ScannerJob] = [
        _ScannerJob(scanner=PendingTasksScanner(), overlay=""),
        _ScannerJob(scanner=IncomingEventsScanner(), overlay=""),
    ]

    if backends:
        for backend in backends:
            tag = backend.name
            if backend.external_db is not None:
                from teatree.loop.scanners.external_tickets import ExternalTicketsScanner  # noqa: PLC0415

                jobs.append(
                    _ScannerJob(
                        scanner=ExternalTicketsScanner(overlay_name=tag, db_path=backend.external_db),
                        overlay=tag,
                    ),
                )
            else:
                jobs.append(_ScannerJob(scanner=ActiveTicketsScanner(overlay_name=tag), overlay=tag))
            jobs.append(
                _ScannerJob(
                    scanner=StaleTicketsScanner(
                        overlay_name=tag,
                        threshold_days=backend.stale_threshold_days,
                    ),
                    overlay=tag,
                ),
            )
            # Multi-host: an overlay with both GitHub and GitLab PATs scans
            # both forges, so PRs on one platform don't drown out PRs on the
            # other (#976). The single-host path is preserved by iterating
            # ``backend.hosts`` (which is empty when no token resolved).
            jobs.extend(_jobs_for_backend_hosts(backend, tag))
            if backend.messaging is not None:
                jobs.extend(
                    [
                        _ScannerJob(scanner=SlackMentionsScanner(backend=backend.messaging), overlay=tag),
                        _ScannerJob(
                            scanner=SlackDmInboundScanner(backend=backend.messaging, overlay=tag),
                            overlay=tag,
                        ),
                        _ScannerJob(
                            scanner=ReviewNagScanner(
                                messaging=backend.messaging,
                                user_slack_id=_user_slack_id_for_overlay(tag),
                            ),
                            overlay=tag,
                        ),
                    ],
                )
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
            jobs.extend(
                [
                    _ScannerJob(scanner=SlackMentionsScanner(backend=messaging), overlay=""),
                    _ScannerJob(scanner=SlackDmInboundScanner(backend=messaging, overlay=""), overlay=""),
                ]
            )

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


def _reap_stale_task_claims() -> None:
    """Recover orphaned ticket state, take over orphaned claims, reap stale ones.

    Three boot/tick recovery sweeps, ordered so a recoverable row is
    rescued before a harsher sweep can fail it. First,
    ``replay_orphaned_transitions`` (#883): a task that COMPLETED but
    whose FSM transition was lost to a mid-transition crash leaves the
    ticket half-advanced; the task is COMPLETED (not CLAIMED) so the
    claim sweeps can't see it and the loop stalls forever — this replays
    the dropped transition via the shared idempotent path. Then
    ``reclaim_orphaned_claims`` (#652): a CLAIMED task whose lease
    expired because its owning session exited mid-task is recoverable —
    returned to PENDING so another still-open session resumes it
    ("fastest open session takes over") rather than being failed.
    Finally ``reap_stale_claims``: any residual stale CLAIMED row is
    failed.

    Best-effort: if the test harness blocks DB access (pytest-django
    without a ``db`` marker), the loop tick should still render scanners
    and signals.
    """
    import contextlib  # noqa: PLC0415

    from teatree.core.models import Task  # noqa: PLC0415

    with contextlib.suppress(RuntimeError):
        Task.objects.replay_orphaned_transitions()
        Task.objects.reclaim_orphaned_claims()
        Task.objects.reap_stale_claims()


def _record_dashboard_actions(report: TickReport, started_at: dt.datetime) -> None:
    """Append one ``tick-actions.jsonl`` line per dispatched action.

    The sidecar is the input the ``t3 loop dashboard`` command reads to
    render the tabular DM. Recording happens here (not in the dashboard
    CLI) so each tick produces ground truth even when the dashboard is
    never displayed — observability is decoupled from rendering.
    """
    from teatree.loop.dashboard import record_actions  # noqa: PLC0415

    try:
        identities = tuple(load_config().user.user_identity_aliases)
    except Exception:  # noqa: BLE001 — config read must never break a tick
        identities = ()
    try:
        record_actions(report.actions, now=started_at, identities=identities)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Recording dashboard actions failed: %s", exc)


def _persist_agent_dispatches(report: TickReport) -> None:
    """Convert ``kind="agent"`` actions into Ticket + Task DB rows.

    The DB is the dispatch queue; the ``/loop`` slot's session reads
    pending Tasks via ``t3 loop pending-spawn`` and spawns sub-agents
    in-session via its ``Agent`` tool. The statusline is purely visual
    and never an orchestration channel.

    Idempotent: if a Ticket already exists for ``(role, issue_url)`` with
    a non-completed reviewing/coding Task, no new rows are created. The
    bidirectional ``ReviewerPrsScanner`` cache (updated when the review
    Task completes) prevents re-spawning at the same SHA.
    """
    from teatree.loop.persistence import persist_agent_actions  # noqa: PLC0415

    try:
        persist_agent_actions(report.actions)
    except Exception as exc:
        logger.exception("Persisting agent dispatches failed")
        report.errors["dispatch_persist"] = f"{type(exc).__name__}: {exc}"


def _execute_mechanical(report: TickReport) -> None:
    """Execute inline mechanical actions (ticket completions, etc.).

    Runs after dispatch but before statusline render so the statusline
    reflects the post-transition state. Errors are captured in
    ``report.errors`` — they never abort the tick.
    """
    from teatree.loop.mechanical import HANDLERS  # noqa: PLC0415

    for action in report.actions:
        if action.kind != "mechanical":
            continue
        handler = HANDLERS.get(action.zone)
        if handler is not None:
            try:
                handler(action.payload)
            except Exception as exc:
                label = f"{action.zone}[{action.payload.get('ticket_id', '?')}]"
                logger.exception("Mechanical action %s failed", label)
                report.errors[label] = f"{type(exc).__name__}: {exc}"


def _repo_freshness(repo_path: Path) -> dict[str, int | str] | None:
    """Snapshot a repo's freshness for the statusline header.

    The ``path`` field is included so the statusline hook can recompute
    ``behind`` inline after a ``git pull`` — otherwise the cached value
    stays stale until the next tick (~12 min later).
    """
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
    return {"behind": behind, "fetch_epoch": fetch_epoch, "path": str(repo_path)}


def _repos_from_toml() -> dict[str, Path]:
    """Extract repo paths from ~/.teatree.toml overlays."""
    toml_path = Path.home() / ".teatree.toml"
    if not toml_path.is_file():
        return {}
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    workspace_dir = Path(str(data.get("teatree", {}).get("workspace_dir", "~/workspace"))).expanduser()
    repos: dict[str, Path] = {}
    for name, overlay in (data.get("overlays") or {}).items():
        if not isinstance(overlay, dict):
            continue
        if "path" in overlay:
            repos[name] = Path(str(overlay["path"])).expanduser()
        for repo_slug in overlay.get("workspace_repos", []):
            if isinstance(repo_slug, str):
                repos[repo_slug.split("/")[-1]] = workspace_dir / repo_slug
    return repos


def _canonical_overlay_names() -> dict[str, str]:
    """Map raw ``~/.teatree.toml`` overlay keys to canonical overlay names.

    The toml entry ``[overlays.teatree]`` corresponds to the canonical
    overlay name ``t3-teatree`` — without this mapping the freshness segment
    would label as ``teatree=0`` even though the rest of the statusline tags
    its rows as ``[t3-teatree]``.
    """
    try:
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    canonical = list(get_all_overlays().keys())
    toml_path = Path.home() / ".teatree.toml"
    if not toml_path.is_file():
        return {}
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    mapping: dict[str, str] = {}
    for raw_key in data.get("overlays") or {}:
        if raw_key in canonical:
            continue
        for cname in canonical:
            if cname == raw_key or cname.endswith((f"-{raw_key}", raw_key)):
                mapping[raw_key] = cname
                break
    return mapping


def _collect_repo_freshness() -> dict[str, dict[str, int | str]]:
    repos: dict[str, Path] = {}
    t3_repo = os.environ.get("T3_REPO")
    if t3_repo:
        repos["t3"] = Path(t3_repo).expanduser()
    repos.update(_repos_from_toml())
    aliases = _canonical_overlay_names()
    return {
        aliases.get(label, label): info for label, path in repos.items() if (info := _repo_freshness(path)) is not None
    }


def _write_tick_meta(started_at: dt.datetime, *, target: Path | None = None) -> None:
    from teatree.loop.statusline import default_path  # noqa: PLC0415

    meta_path = (target or default_path()).with_name("tick-meta.json")
    # #744: the skip path writes tick-meta directly without the
    # render() that side-effect-creates the dir on the normal path —
    # ensure the parent exists so an observability write never crashes
    # the tick (or the skipped-tick freshness touch).
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    cadence = int(os.environ.get("T3_LOOP_CADENCE", "720") or "720")
    next_epoch = int(started_at.timestamp()) + cadence
    freshness = _collect_repo_freshness()
    meta_path.write_text(
        json.dumps({"next_epoch": next_epoch, "cadence": cadence, "freshness": freshness}) + "\n",
        encoding="utf-8",
    )
